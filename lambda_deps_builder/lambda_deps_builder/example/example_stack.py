from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from constructs import Construct

from lambda_deps_builder import LambdaDepsBuilder

_HERE = Path(__file__).parent
_REQUIREMENTS = _HERE / "requirements_to_build.txt"
_CONSUMER_DIR = _HERE / "consumer_handler"


class ExampleStack(Stack):
    """Builds `requests` for both x86_64 and arm64 and wires a consumer Lambda per arch."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        shared_bucket = s3.Bucket(
            self,
            "DepsBucket",
            auto_delete_objects=True,
            removal_policy=RemovalPolicy.DESTROY,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )

        for arch_name, arch in [
            ("X86", lambda_.Architecture.X86_64),
            ("Arm", lambda_.Architecture.ARM_64),
        ]:
            builder = LambdaDepsBuilder(
                self,
                f"RequestsDeps{arch_name}",
                requirements_txt_file=_REQUIREMENTS,
                target_runtime=lambda_.Runtime.PYTHON_3_12,
                target_architecture=arch,
                deps_bucket=shared_bucket,
            )
            layer = builder.as_layer_version(self, f"RequestsLayer{arch_name}")

            consumer = lambda_.Function(
                self,
                f"Consumer{arch_name}",
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=arch,
                handler="handler.handler",
                code=lambda_.Code.from_asset(str(_CONSUMER_DIR)),
                layers=[layer],
                timeout=Duration.seconds(15),
            )
            CfnOutput(
                self,
                f"Consumer{arch_name}FunctionName",
                value=consumer.function_name,
            )
