from pathlib import Path

import pytest
from aws_cdk import Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Match, Template

import aws_cdk as cdk

from lambda_deps_builder import LambdaDepsBuilder


def _synth_stack(builder_factory) -> Template:
    app = cdk.App()
    stack = Stack(app, "TestStack")
    builder_factory(stack)
    return Template.from_stack(stack)


def test_default_synthesizes_bucket_trigger_and_layer(tmp_requirements_file: Path) -> None:
    def factory(stack: Stack) -> None:
        b = LambdaDepsBuilder(
            stack,
            "Deps",
            requirements_txt_file=tmp_requirements_file,
        )
        b.as_layer_version(stack, "Layer")

    template = _synth_stack(factory)

    template.resource_count_is("AWS::S3::Bucket", 1)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like({"Runtime": "python3.12", "Architectures": ["x86_64"]}),
    )
    template.has_resource_properties(
        "AWS::Lambda::LayerVersion",
        Match.object_like(
            {
                "CompatibleRuntimes": ["python3.12"],
                "CompatibleArchitectures": ["x86_64"],
                "Content": Match.object_like({"S3Key": "deps-x86_64-Deps.zip"}),
            }
        ),
    )


def test_arm64_propagates_to_function_and_layer(tmp_requirements_file: Path) -> None:
    def factory(stack: Stack) -> None:
        b = LambdaDepsBuilder(
            stack,
            "DepsArm",
            requirements_txt_file=tmp_requirements_file,
            target_architecture=lambda_.Architecture.ARM_64,
        )
        b.as_layer_version(stack, "LayerArm")

    template = _synth_stack(factory)

    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like({"Architectures": ["arm64"]}),
    )
    template.has_resource_properties(
        "AWS::Lambda::LayerVersion",
        Match.object_like(
            {
                "CompatibleArchitectures": ["arm64"],
                "Content": Match.object_like({"S3Key": "deps-arm64-DepsArm.zip"}),
            }
        ),
    )


def test_layer_depends_on_trigger_custom_resource(tmp_requirements_file: Path) -> None:
    def factory(stack: Stack) -> None:
        b = LambdaDepsBuilder(
            stack,
            "Deps",
            requirements_txt_file=tmp_requirements_file,
        )
        b.as_layer_version(stack, "Layer")

    template = _synth_stack(factory)

    layer_resources = template.find_resources("AWS::Lambda::LayerVersion")
    assert len(layer_resources) == 1
    layer = next(iter(layer_resources.values()))
    depends_on = layer.get("DependsOn", [])
    trigger_custom_resources = template.find_resources("Custom::Trigger")
    assert trigger_custom_resources, "expected a Custom::Trigger custom resource"
    trigger_logical_ids = set(trigger_custom_resources.keys())
    assert trigger_logical_ids.intersection(depends_on), (
        f"layer DependsOn {depends_on} should include the trigger custom resource "
        f"{trigger_logical_ids}"
    )


def test_trigger_role_can_put_object_on_bucket(tmp_requirements_file: Path) -> None:
    def factory(stack: Stack) -> None:
        LambdaDepsBuilder(
            stack,
            "Deps",
            requirements_txt_file=tmp_requirements_file,
        )

    template = _synth_stack(factory)

    template.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Action": Match.array_with(["s3:PutObject"]),
                                        "Effect": "Allow",
                                    }
                                )
                            ]
                        )
                    }
                )
            }
        ),
    )


def test_external_bucket_is_reused(tmp_requirements_file: Path) -> None:
    def factory(stack: Stack) -> None:
        bucket = s3.Bucket(stack, "External")
        LambdaDepsBuilder(
            stack,
            "Deps",
            requirements_txt_file=tmp_requirements_file,
            deps_bucket=bucket,
        )

    template = _synth_stack(factory)

    template.resource_count_is("AWS::S3::Bucket", 1)


def test_two_architectures_share_bucket_with_distinct_keys(
    tmp_requirements_file: Path,
) -> None:
    def factory(stack: Stack) -> None:
        bucket = s3.Bucket(stack, "Shared")
        LambdaDepsBuilder(
            stack,
            "DepsX86",
            requirements_txt_file=tmp_requirements_file,
            target_architecture=lambda_.Architecture.X86_64,
            deps_bucket=bucket,
        )
        LambdaDepsBuilder(
            stack,
            "DepsArm",
            requirements_txt_file=tmp_requirements_file,
            target_architecture=lambda_.Architecture.ARM_64,
            deps_bucket=bucket,
        )

    template = _synth_stack(factory)

    template.resource_count_is("AWS::S3::Bucket", 1)
    s3_keys: set = set()
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action")
            actions = actions if isinstance(actions, list) else [actions]
            if "s3:PutObject" not in actions:
                continue
            for resource_entry in _iter_resource_entries(statement.get("Resource")):
                key = _extract_object_key(resource_entry)
                if key is not None:
                    s3_keys.add(key)
    assert {"deps-x86_64-DepsX86.zip", "deps-arm64-DepsArm.zip"}.issubset(s3_keys), (
        f"expected per-architecture keys in PutObject grants, got {s3_keys}"
    )


def test_custom_output_key_overrides_default(tmp_requirements_file: Path) -> None:
    def factory(stack: Stack) -> None:
        b = LambdaDepsBuilder(
            stack,
            "Deps",
            requirements_txt_file=tmp_requirements_file,
            output_key="custom/path.zip",
        )
        b.as_layer_version(stack, "Layer")

    template = _synth_stack(factory)

    template.has_resource_properties(
        "AWS::Lambda::LayerVersion",
        Match.object_like({"Content": Match.object_like({"S3Key": "custom/path.zip"})}),
    )


def test_build_settings_propagate(tmp_requirements_file: Path) -> None:
    def factory(stack: Stack) -> None:
        LambdaDepsBuilder(
            stack,
            "Deps",
            requirements_txt_file=tmp_requirements_file,
            build_timeout=cdk.Duration.minutes(7),
            build_memory_mb=2048,
            ephemeral_storage_gib=6,
        )

    template = _synth_stack(factory)

    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like(
            {
                "Timeout": 420,
                "MemorySize": 2048,
                "EphemeralStorage": {"Size": 6 * 1024},
            }
        ),
    )


def test_same_arch_different_ids_get_distinct_keys(tmp_requirements_file: Path) -> None:
    def factory(stack: Stack) -> None:
        bucket = s3.Bucket(stack, "Shared")
        LambdaDepsBuilder(
            stack,
            "ApiDeps",
            requirements_txt_file=tmp_requirements_file,
            deps_bucket=bucket,
        )
        LambdaDepsBuilder(
            stack,
            "WorkerDeps",
            requirements_txt_file=tmp_requirements_file,
            deps_bucket=bucket,
        )

    template = _synth_stack(factory)

    s3_keys: set = set()
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action")
            actions = actions if isinstance(actions, list) else [actions]
            if "s3:PutObject" not in actions:
                continue
            for resource_entry in _iter_resource_entries(statement.get("Resource")):
                key = _extract_object_key(resource_entry)
                if key is not None:
                    s3_keys.add(key)
    assert {"deps-x86_64-ApiDeps.zip", "deps-x86_64-WorkerDeps.zip"}.issubset(s3_keys), (
        f"same-arch builders must get unique keys, got {s3_keys}"
    )


def test_empty_compatible_runtimes_is_forwarded_to_cdk(tmp_requirements_file: Path) -> None:
    """An explicit [] must not collapse to the default — it should reach CDK as []
    (which CDK itself rejects with a clear message). Previously a falsy `or`
    silently substituted [target_runtime] and the user's intent was lost."""

    app = cdk.App()
    stack = Stack(app, "S")
    b = LambdaDepsBuilder(stack, "Deps", requirements_txt_file=tmp_requirements_file)
    with pytest.raises(Exception, match="supports no runtime"):
        b.as_layer_version(stack, "Layer", compatible_runtimes=[])


def test_attach_to_function_adds_dependency_on_trigger(tmp_requirements_file: Path) -> None:
    def factory(stack: Stack) -> None:
        b = LambdaDepsBuilder(
            stack,
            "Deps",
            requirements_txt_file=tmp_requirements_file,
        )
        fn = lambda_.Function(
            stack,
            "Consumer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=b.as_function_code(),
        )
        b.attach_to_function(fn)

    template = _synth_stack(factory)

    triggers_in_template = template.find_resources("Custom::Trigger")
    assert triggers_in_template
    consumer_resources = {
        k: v
        for k, v in template.find_resources("AWS::Lambda::Function").items()
        if k.startswith("Consumer")
    }
    assert len(consumer_resources) == 1
    consumer = next(iter(consumer_resources.values()))
    depends_on = set(consumer.get("DependsOn", []))
    assert set(triggers_in_template.keys()) & depends_on, (
        f"Consumer DependsOn {depends_on} should include a trigger custom resource"
    )


def test_missing_requirements_file_raises(tmp_path: Path) -> None:
    app = cdk.App()
    stack = Stack(app, "Bad")
    with pytest.raises(FileNotFoundError):
        LambdaDepsBuilder(
            stack,
            "Deps",
            requirements_txt_file=tmp_path / "does-not-exist.txt",
        )


def _iter_resource_entries(resource):
    if resource is None:
        return
    if isinstance(resource, list):
        for r in resource:
            yield r
    else:
        yield resource


def _extract_object_key(resource_entry):
    """Pull the trailing object key out of a CFN-intrinsic ARN expression like:
    {"Fn::Join": ["", [{"Fn::GetAtt": [...]}, "/deps-X86_64.zip"]]}."""
    if not isinstance(resource_entry, dict):
        return None
    join = resource_entry.get("Fn::Join")
    if not join or len(join) != 2:
        return None
    parts = join[1]
    for part in parts:
        if isinstance(part, str) and part.startswith("/"):
            return part.lstrip("/")
    return None
