import atexit
import shutil
import tempfile
from pathlib import Path
from typing import Optional, List

from aws_cdk import Duration, RemovalPolicy, Size
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
    ) -> None:
        super().__init__(scope, construct_id)

        if not requirements_txt_file.is_file():
            raise FileNotFoundError(
                f"requirements_txt_file does not exist: {requirements_txt_file}"
            )

        self._target_architecture = target_architecture
        self._deps_key = (
            output_key
            if output_key is not None
            else f"deps-{target_architecture.name}-{construct_id}.zip"
        )
        self._target_runtime = target_runtime

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

        staged_asset_dir = _stage_handler_asset(requirements_txt_file)

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
            environment={
                "BUCKET_NAME": deps_bucket.bucket_name,
                "OBJECT_KEY": self._deps_key,
            },
        )
        deps_bucket.grant_put(self._trigger, objects_key_pattern=self._deps_key)

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


def _stage_handler_asset(requirements_txt_file: Path) -> str:
    """
    Copy the in-Lambda handler module and the user's requirements file into a fresh
    directory so they can be packaged as a single asset. The directory contents
    determine the asset hash, so changes to requirements.txt re-fire the trigger.

    The temp directory is registered for cleanup at process exit, since CDK only
    needs to read it during synth.

    :param requirements_txt_file: user-supplied requirements file.
    :return: path to the staged directory as a string for `Code.from_asset`.
    """
    staged_dir = Path(tempfile.mkdtemp(prefix="lambda-deps-builder-"))
    atexit.register(shutil.rmtree, staged_dir, ignore_errors=True)
    shutil.copy(_HANDLER_SOURCE_DIR / "handler.py", staged_dir / "handler.py")
    shutil.copy(requirements_txt_file, staged_dir / "requirements.txt")
    return str(staged_dir)
