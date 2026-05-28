"""End-to-end synth of the example stack.

Mirrors the manual checks performed during development: one shared bucket, two builder
Lambdas (one per architecture), two layers with matching `CompatibleArchitectures`
and distinct per-arch S3 keys, two consumer Lambdas on matching architectures, and
each layer's `DependsOn` containing its own trigger custom resource.
"""

import json
from pathlib import Path

import aws_cdk as cdk
import pytest

from lambda_deps_builder.example.example_stack import ExampleStack


@pytest.fixture(scope="module")
def synthesized_template() -> dict:
    """Synth the real ExampleStack once and return the parsed CFN template."""
    app = cdk.App()
    ExampleStack(app, "LambdaDepsBuilderExample")
    assembly = app.synth()
    artifact = assembly.get_stack_by_name("LambdaDepsBuilderExample")
    return json.loads(Path(artifact.template_full_path).read_text())


def _resources_of_type(template: dict, type_name: str) -> dict:
    return {
        k: v for k, v in template["Resources"].items() if v["Type"] == type_name
    }


def _user_lambda_functions(template: dict) -> dict:
    """Lambda functions defined by the example, excluding CDK-internal helpers."""
    cdk_internal_substrings = (
        "CustomS3AutoDeleteObjects",
        "AWSCDKTriggerCustomResourceProvider",
    )
    return {
        k: v
        for k, v in _resources_of_type(template, "AWS::Lambda::Function").items()
        if not any(s in k for s in cdk_internal_substrings)
    }


def test_one_shared_bucket(synthesized_template: dict) -> None:
    assert len(_resources_of_type(synthesized_template, "AWS::S3::Bucket")) == 1


def test_two_builders_one_per_architecture(synthesized_template: dict) -> None:
    builders = {
        k: v
        for k, v in _user_lambda_functions(synthesized_template).items()
        if "Builder" in k
    }
    assert len(builders) == 2, f"expected 2 builders, found {list(builders)}"
    architectures = sorted(b["Properties"]["Architectures"][0] for b in builders.values())
    assert architectures == ["arm64", "x86_64"]


def test_two_consumers_one_per_architecture(synthesized_template: dict) -> None:
    consumers = {
        k: v
        for k, v in _user_lambda_functions(synthesized_template).items()
        if k.startswith("Consumer")
    }
    assert len(consumers) == 2, f"expected 2 consumers, found {list(consumers)}"
    architectures = sorted(c["Properties"]["Architectures"][0] for c in consumers.values())
    assert architectures == ["arm64", "x86_64"]


def test_layers_have_matching_architecture_and_distinct_keys(
    synthesized_template: dict,
) -> None:
    layers = _resources_of_type(synthesized_template, "AWS::Lambda::LayerVersion")
    assert len(layers) == 2

    pairs = {
        layer["Properties"]["CompatibleArchitectures"][0]: layer["Properties"][
            "Content"
        ]["S3Key"]
        for layer in layers.values()
    }
    assert pairs == {
        "x86_64": "deps-x86_64-RequestsDepsX86.zip",
        "arm64": "deps-arm64-RequestsDepsArm.zip",
    }


def test_each_layer_depends_on_its_own_trigger(synthesized_template: dict) -> None:
    triggers = set(_resources_of_type(synthesized_template, "Custom::Trigger").keys())
    assert len(triggers) == 2, f"expected 2 trigger custom resources, found {triggers}"

    layers = _resources_of_type(synthesized_template, "AWS::Lambda::LayerVersion")
    for logical_id, layer in layers.items():
        depends_on = set(layer.get("DependsOn", []))
        intersection = triggers & depends_on
        assert intersection, (
            f"layer {logical_id} DependsOn {depends_on} should include one of "
            f"{triggers}"
        )


def test_consumer_layer_arch_matches_consumer_arch(synthesized_template: dict) -> None:
    """A Consumer's `Layers` ref must point to a layer of the consumer's architecture."""
    layers = _resources_of_type(synthesized_template, "AWS::Lambda::LayerVersion")
    layer_arch = {
        logical_id: layer["Properties"]["CompatibleArchitectures"][0]
        for logical_id, layer in layers.items()
    }

    consumers = {
        k: v
        for k, v in _user_lambda_functions(synthesized_template).items()
        if k.startswith("Consumer")
    }
    for consumer_id, consumer in consumers.items():
        consumer_arch = consumer["Properties"]["Architectures"][0]
        layer_refs = consumer["Properties"]["Layers"]
        assert len(layer_refs) == 1
        layer_logical_id = layer_refs[0]["Ref"]
        assert layer_arch[layer_logical_id] == consumer_arch, (
            f"consumer {consumer_id} on {consumer_arch} attached layer "
            f"{layer_logical_id} of arch {layer_arch[layer_logical_id]}"
        )


def test_consumer_runtime_matches_layer_runtime(synthesized_template: dict) -> None:
    layers = _resources_of_type(synthesized_template, "AWS::Lambda::LayerVersion")
    consumers = {
        k: v
        for k, v in _user_lambda_functions(synthesized_template).items()
        if k.startswith("Consumer")
    }
    for consumer in consumers.values():
        runtime = consumer["Properties"]["Runtime"]
        layer_logical_id = consumer["Properties"]["Layers"][0]["Ref"]
        compatible_runtimes = layers[layer_logical_id]["Properties"][
            "CompatibleRuntimes"
        ]
        assert runtime in compatible_runtimes


def test_outputs_expose_consumer_function_names(synthesized_template: dict) -> None:
    outputs = synthesized_template.get("Outputs", {})
    assert "ConsumerX86FunctionName" in outputs
    assert "ConsumerArmFunctionName" in outputs


def test_builder_handler_env_carries_bucket_and_key(synthesized_template: dict) -> None:
    builders = {
        k: v
        for k, v in _user_lambda_functions(synthesized_template).items()
        if "Builder" in k
    }
    for logical_id, builder in builders.items():
        env = builder["Properties"]["Environment"]["Variables"]
        assert "BUCKET_NAME" in env, f"{logical_id} missing BUCKET_NAME"
        assert env["OBJECT_KEY"] in {
            "deps-x86_64-RequestsDepsX86.zip",
            "deps-arm64-RequestsDepsArm.zip",
        }, f"{logical_id} unexpected OBJECT_KEY: {env['OBJECT_KEY']}"
