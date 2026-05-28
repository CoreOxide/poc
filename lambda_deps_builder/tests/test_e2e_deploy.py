"""End-to-end test against real AWS.

Deploys the example stack, invokes both consumer Lambdas, and asserts each one
successfully imports `requests` (built on Linux Lambda, not the dev machine) and
hits https://example.com.

Run via `poetry run pytest -m e2e`. Requires:
    - AWS credentials with permission to create the stack's resources
    - `cdk` CLI on PATH
    - The target account/region bootstrapped (`cdk bootstrap` once)

The stack uses a unique name per run so concurrent runs don't collide, and is
destroyed in a finally block so a failed deploy or assertion still cleans up.
"""

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterator

import boto3
import pytest

pytestmark = pytest.mark.e2e

_PROJECT_DIR = Path(__file__).resolve().parent.parent


def _resolve_region() -> str:
    """Resolve the deploy region the same way CDK does, with a sensible fallback."""
    return (
        os.environ.get("CDK_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or boto3.session.Session().region_name
        or "us-east-1"
    )


@pytest.fixture(scope="module")
def deployed_stack() -> Iterator[dict]:
    if shutil.which("cdk") is None:
        pytest.skip("cdk CLI not on PATH")

    region = _resolve_region()
    stack_name = f"LambdaDepsBuilderE2E-{uuid.uuid4().hex[:8]}"
    env = {
        **os.environ,
        "STACK_NAME": stack_name,
        "CDK_DEFAULT_REGION": region,
        "AWS_REGION": region,
    }

    deployed = False
    try:
        print(f"\n[E2E] deploying stack {stack_name} in {region} ...")
        deploy_start = time.monotonic()
        subprocess.run(
            ["cdk", "deploy", "--require-approval", "never", "--ci"],
            cwd=_PROJECT_DIR,
            env=env,
            check=True,
        )
        print(f"[E2E] deploy took {time.monotonic() - deploy_start:.0f}s")
        deployed = True

        cfn = boto3.client("cloudformation", region_name=region)
        outputs = {
            o["OutputKey"]: o["OutputValue"]
            for o in cfn.describe_stacks(StackName=stack_name)["Stacks"][0].get(
                "Outputs", []
            )
        }
        yield {"stack_name": stack_name, "outputs": outputs, "region": region}
    finally:
        if deployed or shutil.which("cdk") is not None:
            print(f"\n[E2E] destroying stack {stack_name} ...")
            subprocess.run(
                ["cdk", "destroy", "--force", "--ci"],
                cwd=_PROJECT_DIR,
                env=env,
                check=False,
            )


@pytest.mark.parametrize("output_key", ["ConsumerX86FunctionName", "ConsumerArmFunctionName"])
def test_consumer_can_import_built_dependency(deployed_stack: dict, output_key: str) -> None:
    function_name = deployed_stack["outputs"][output_key]

    response = boto3.client("lambda", region_name=deployed_stack["region"]).invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=b"{}",
    )
    payload_bytes = response["Payload"].read()

    assert response.get("FunctionError") is None, (
        f"{output_key} returned FunctionError: {payload_bytes.decode(errors='replace')}"
    )
    payload = json.loads(payload_bytes)
    assert payload["status"] == 200, payload
    assert payload["len"] > 0, payload
