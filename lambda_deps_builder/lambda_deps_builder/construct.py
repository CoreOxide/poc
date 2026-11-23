import atexit
import json
import shutil
import tempfile
from pathlib import Path
from typing import Optional, List, Literal

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Size
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import triggers
from constructs import Construct

_HANDLER_SOURCE_DIR = Path(__file__).parent / "builder_handler"


class LambdaDepsBuilder(Construct):
    """
    Builds Python Lambda dependencies inside an AWS Lambda matching the target runtime
    and architecture, uploads the resulting layer-shaped zip to S3, and exposes it as
    `aws_lambda.Code` / `aws_lambda.LayerVersion` for use in the same stack — no local
    Docker required.

    :param scope: parent construct.
    :param construct_id: construct id.
    :param requirements_txt_file: path on the developer machine to a requirements.txt
        whose dependencies should be installed inside Lambda.
    :param target_runtime: the Lambda runtime to build for. Pip resolves wheels matching
        this runtime's Python version.
    :param target_architecture: the Lambda architecture to build for. The build runs on
        a Lambda of this architecture so pip selects matching wheels (x86_64 vs arm64).
    :param deps_bucket: optional pre-existing bucket to upload into. A bucket is
        auto-created when omitted. NOTE: a caller-supplied bucket without
        ``auto_delete_objects=True`` will fail to destroy because the trigger leaves
        the zip behind; ensure the bucket can be emptied.
    :param output_key: S3 key for the produced zip. Defaults to
        ``deps-{ARCH}-{construct_id}.zip`` so sibling builders sharing a bucket never
        collide regardless of architecture or requirements.
    :param build_timeout: timeout for the build Lambda. Increase for large requirement
        sets that take long to install.
    :param build_memory_mb: memory for the build Lambda.
    :param ephemeral_storage_gib: size of `/tmp` inside the build Lambda. Lambda's
        default is 0.5 GiB; raise for large dep trees.
    :param build_engine: package installation engine to run inside Lambda ("uv" or "pip").
        Defaults to "uv" for 10x faster builds.
    :param uv_package_spec: package specification used to install uv when not bundled or
        cached (e.g. "uv", "uv>=0.5"). Defaults to "uv".
    :param uv_binary_path: optional local path to a pre-compiled Linux uv binary. When
        provided, the binary is bundled directly into the Lambda asset, skipping runtime
        installation.
    :param fallback_to_pip: whether to fall back to standard pip install if uv installation
        or execution fails inside Lambda. Defaults to True.
    :param generate_size_report: whether to analyze installed package sizes inside Lambda
        and upload a self-contained interactive HTML treemap report to S3. Defaults to True.
    :param report_output_key: S3 key for the HTML treemap report. Defaults to
        ``layer-analysis-{ARCH}-{construct_id}.html``.
    :param emit_report_cfn_output: whether to emit a CloudFormation Output containing the
        S3 URI of the generated HTML treemap report. Defaults to True.
    :param slim_layer: whether to automatically prune non-runtime bloat (bytecode caches,
        tests, docs, stubs) before zipping. Defaults to True.
    :param strip_binaries: whether to run ``strip --strip-unneeded`` on ``.so`` shared
        libraries when a ``strip`` binary is available. Defaults to True.
    :param strip_binary_path: optional local path to a Linux ``strip`` binary to bundle
        into the Lambda asset at ``bin/strip``.
    :param prune_tests: whether to remove ``tests/`` and ``test/`` directories during
        slimming (preserves ``testing/``). Defaults to True.
    :param prune_dist_info: whether to remove ``*.dist-info`` and ``*.egg-info``
        metadata directories during slimming. Defaults to False to preserve
        ``importlib.metadata``.
    :param exclude_patterns: optional list of glob patterns (matched against relative
        POSIX paths or basenames) to remove during slimming.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        requirements_txt_file: Path,
        target_runtime: lambda_.Runtime = lambda_.Runtime.PYTHON_3_12,
        target_architecture: lambda_.Architecture = lambda_.Architecture.X86_64,
        deps_bucket: Optional[s3.IBucket] = None,
        output_key: Optional[str] = None,
        build_timeout: Duration = Duration.minutes(5),
        build_memory_mb: int = 1024,
        ephemeral_storage_gib: int = 4,
        build_engine: Literal["uv", "pip"] = "uv",
        uv_package_spec: str = "uv",
        uv_binary_path: Optional[Path] = None,
        fallback_to_pip: bool = True,
        generate_size_report: bool = True,
        report_output_key: Optional[str] = None,
        emit_report_cfn_output: bool = True,
        slim_layer: bool = True,
        strip_binaries: bool = True,
        strip_binary_path: Optional[Path] = None,
        prune_tests: bool = True,
        prune_dist_info: bool = False,
        exclude_patterns: Optional[List[str]] = None,
    ) -> None:
        super().__init__(scope, construct_id)

        if not requirements_txt_file.is_file():
            raise FileNotFoundError(
                f"requirements_txt_file does not exist: {requirements_txt_file}"
            )

        if build_engine not in ("uv", "pip"):
            raise ValueError(f"build_engine must be 'uv' or 'pip', got '{build_engine}'")

        if uv_binary_path is not None and not uv_binary_path.is_file():
            raise FileNotFoundError(
                f"uv_binary_path does not exist: {uv_binary_path}"
            )

        if strip_binary_path is not None and not strip_binary_path.is_file():
            raise FileNotFoundError(
                f"strip_binary_path does not exist: {strip_binary_path}"
            )

        self._target_architecture = target_architecture
        self._deps_key = (
            output_key
            if output_key is not None
            else f"deps-{target_architecture.name}-{construct_id}.zip"
        )
        self._target_runtime = target_runtime
        self._build_engine = build_engine
        self._uv_package_spec = uv_package_spec
        self._fallback_to_pip = fallback_to_pip
        self._generate_size_report = generate_size_report
        self._report_key: Optional[str] = (
            (
                report_output_key
                if report_output_key is not None
                else f"layer-analysis-{target_architecture.name}-{construct_id}.html"
            )
            if generate_size_report
            else None
        )
        self._slim_layer = slim_layer
        self._strip_binaries = strip_binaries
        self._prune_tests = prune_tests
        self._prune_dist_info = prune_dist_info
        self._exclude_patterns = (
            list(exclude_patterns) if exclude_patterns is not None else []
        )

        if deps_bucket is None:
            deps_bucket = s3.Bucket(
                self,
                "DepsBucket",
                auto_delete_objects=True,
                removal_policy=RemovalPolicy.DESTROY,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                enforce_ssl=True,
            )
        self._deps_bucket = deps_bucket

        staged_asset_dir = _stage_handler_asset(
            requirements_txt_file,
            uv_binary_path=uv_binary_path,
            strip_binary_path=strip_binary_path,
        )

        trigger_env = {
            "BUCKET_NAME": deps_bucket.bucket_name,
            "OBJECT_KEY": self._deps_key,
            "BUILD_ENGINE": self._build_engine,
            "UV_PACKAGE_SPEC": self._uv_package_spec,
            "FALLBACK_TO_PIP": "1" if self._fallback_to_pip else "0",
            "GENERATE_SIZE_REPORT": "1" if self._generate_size_report else "0",
            "TARGET_RUNTIME": target_runtime.name,
            "TARGET_ARCHITECTURE": target_architecture.name,
            "SLIM_LAYER": "1" if self._slim_layer else "0",
            "STRIP_BINARIES": "1" if self._strip_binaries else "0",
            "PRUNE_TESTS": "1" if self._prune_tests else "0",
            "PRUNE_DIST_INFO": "1" if self._prune_dist_info else "0",
            "EXCLUDE_PATTERNS": json.dumps(self._exclude_patterns),
        }
        if self._report_key is not None:
            trigger_env["REPORT_OBJECT_KEY"] = self._report_key

        self._trigger = triggers.TriggerFunction(
            self,
            "Builder",
            runtime=target_runtime,
            architecture=target_architecture,
            handler="handler.handler",
            code=lambda_.Code.from_asset(staged_asset_dir),
            timeout=build_timeout,
            memory_size=build_memory_mb,
            ephemeral_storage_size=Size.gibibytes(ephemeral_storage_gib),
            environment=trigger_env,
        )
        deps_bucket.grant_put(self._trigger, objects_key_pattern=self._deps_key)
        if self._report_key is not None:
            deps_bucket.grant_put(self._trigger, objects_key_pattern=self._report_key)

        if (
            self._generate_size_report
            and emit_report_cfn_output
            and self._report_key is not None
        ):
            CfnOutput(
                self,
                "LayerAnalysisReportUri",
                value=f"s3://{deps_bucket.bucket_name}/{self._report_key}",
                description="S3 URI of the interactive HTML layer size treemap report",
            )

    @property
    def target_architecture(self) -> lambda_.Architecture:
        return self._target_architecture

    @property
    def deps_bucket(self) -> s3.IBucket:
        return self._deps_bucket

    @property
    def deps_key(self) -> str:
        return self._deps_key

    @property
    def trigger(self) -> triggers.TriggerFunction:
        return self._trigger

    @property
    def build_engine(self) -> str:
        return self._build_engine

    @property
    def uv_package_spec(self) -> str:
        return self._uv_package_spec

    @property
    def fallback_to_pip(self) -> bool:
        return self._fallback_to_pip

    @property
    def generate_size_report(self) -> bool:
        return self._generate_size_report

    @property
    def report_key(self) -> Optional[str]:
        return self._report_key

    @property
    def report_s3_uri(self) -> Optional[str]:
        if self._report_key is None:
            return None
        return f"s3://{self._deps_bucket.bucket_name}/{self._report_key}"

    @property
    def slim_layer(self) -> bool:
        return self._slim_layer

    @property
    def strip_binaries(self) -> bool:
        return self._strip_binaries

    @property
    def prune_tests(self) -> bool:
        return self._prune_tests

    @property
    def prune_dist_info(self) -> bool:
        return self._prune_dist_info

    @property
    def exclude_patterns(self) -> List[str]:
        return list(self._exclude_patterns)

    def as_layer_version(
        self,
        scope: Construct,
        construct_id: str,
        *,
        compatible_runtimes: Optional[List[lambda_.Runtime]] = None,
    ) -> lambda_.LayerVersion:
        """
        Build a `LayerVersion` whose code is the S3 zip produced by the trigger.

        :param scope: parent construct for the LayerVersion.
        :param construct_id: construct id for the LayerVersion.
        :param compatible_runtimes: optional override; defaults to `[target_runtime]`.
            Pass an empty list to publish a layer with no runtime restriction.
        :return: a LayerVersion that depends on the trigger so CFN waits for the build.
        """
        runtimes = (
            compatible_runtimes
            if compatible_runtimes is not None
            else [self._target_runtime]
        )
        layer = lambda_.LayerVersion(
            scope,
            construct_id,
            code=lambda_.Code.from_bucket(self._deps_bucket, self._deps_key),
            compatible_runtimes=runtimes,
            compatible_architectures=[self._target_architecture],
        )
        layer.node.add_dependency(self._trigger)
        return layer

    def as_function_code(self) -> lambda_.Code:
        """
        Return a `lambda_.Code` referencing the built S3 zip. Prefer
        :meth:`attach_to_function`, which also wires the CDK dependency on the trigger.
        Use this lower-level helper only when you can't access the consuming Function
        at the call site, and remember to call ``fn.node.add_dependency(self.trigger)``
        yourself or CFN may try to create the Function before the upload exists.
        """
        return lambda_.Code.from_bucket(self._deps_bucket, self._deps_key)

    def attach_to_function(self, function: lambda_.Function) -> None:
        """
        Wire an existing Function's code to the built deps zip and add the CDK
        dependency on the trigger so CFN creates the Function only after the upload.

        :param function: the Function whose code should be the built deps zip.
        """
        function.node.add_dependency(self._trigger)


def _stage_handler_asset(
    requirements_txt_file: Path,
    uv_binary_path: Optional[Path] = None,
    strip_binary_path: Optional[Path] = None,
) -> str:
    """
    Copy the in-Lambda handler module, slimmer module, and the user's requirements
    file into a fresh directory so they can be packaged as a single asset. The directory
    contents determine the asset hash, so changes to requirements.txt re-fire the trigger.

    If `uv_binary_path` is provided, stages the binary into `bin/uv` within the asset
    directory so it is bundled directly with the trigger function.
    If `strip_binary_path` is provided, stages the binary into `bin/strip`.

    The temp directory is registered for cleanup at process exit, since CDK only
    needs to read it during synth.

    :param requirements_txt_file: user-supplied requirements file.
    :param uv_binary_path: optional local pre-compiled Linux uv binary.
    :param strip_binary_path: optional local pre-compiled Linux strip binary.
    :return: path to the staged directory as a string for `Code.from_asset`.
    """
    staged_dir = Path(tempfile.mkdtemp(prefix="lambda-deps-builder-"))
    atexit.register(shutil.rmtree, staged_dir, ignore_errors=True)
    shutil.copy(_HANDLER_SOURCE_DIR / "handler.py", staged_dir / "handler.py")
    shutil.copy(_HANDLER_SOURCE_DIR / "analyzer.py", staged_dir / "analyzer.py")
    shutil.copy(_HANDLER_SOURCE_DIR / "slimmer.py", staged_dir / "slimmer.py")
    shutil.copy(requirements_txt_file, staged_dir / "requirements.txt")
    if uv_binary_path is not None:
        bin_dir = staged_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(uv_binary_path, bin_dir / "uv")
    if strip_binary_path is not None:
        bin_dir = staged_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(strip_binary_path, bin_dir / "strip")
    return str(staged_dir)
