# lambda_deps_builder

A CDK construct that builds Python Lambda dependencies **inside an actual AWS Lambda** during `cdk deploy` — no local Docker, no platform mismatch, no cross-compile gymnastics.

## What it solves

Building Python Lambda packages on Windows or on a developer machine without Docker produces wheels that fail on Lambda's Linux runtime. The standard CDK answers all have caveats:

| Approach | Where deps are resolved | Local prerequisites | Cross-platform safe? |
|---|---|---|---|
| `lambda.Code.from_asset(local_pip_install_dir)` | Dev machine | Python + pip | **No** — native wheels often wrong |
| `aws_lambda_python_alpha.PythonFunction` | Local Docker container | **Docker daemon** | Yes (Docker does the cross-build) |
| `aws_lambda_nodejs.NodejsFunction` | Local esbuild (or Docker fallback) | Node.js + esbuild (or Docker) | Yes (JS is mostly platform-agnostic) |
| **`LambdaDepsBuilder` (this POC)** | **Inside AWS Lambda, during `cdk deploy`** | **None beyond CDK** | Yes — by definition |

The build runs on the same Lambda runtime + architecture that will execute the deps, so `pip` selects matching wheels automatically.

## Architecture

```
synth ──> CDK creates: S3 bucket
                       + TriggerFunction(runtime=python3.12, architecture=X)
                       + LayerVersion(code=fromBucket, compatible_architectures=[X])
deploy ──> 1. CFN creates bucket
           2. CFN creates the TriggerFunction (handler asset bundles requirements.txt)
           3. Triggers framework invokes the function ONCE, synchronously, on AWS Lambda
              └─ handler runs ON ARCHITECTURE X:
                                pip install -r requirements.txt -t /tmp/build/python
                                zip /tmp/build  →  /tmp/deps.zip
                                s3.put_object(Bucket=..., Key=deps-X.zip)
           4. CFN creates LayerVersion (depends_on the trigger) → reads zip from S3
           5. Consumer Function(architecture=X, layers=[layer]) runs with matching deps
```

The trigger re-fires whenever `requirements.txt` changes (its content is part of the staged asset hash) or whenever the architecture changes.

## Usage

```python
from pathlib import Path
from aws_cdk import Stack, Duration
from aws_cdk import aws_lambda as lambda_
from lambda_deps_builder import LambdaDepsBuilder

class MyStack(Stack):
    def __init__(self, scope, id, **kwargs):
        super().__init__(scope, id, **kwargs)

        builder = LambdaDepsBuilder(
            self, "Deps",
            requirements_txt_file=Path(__file__).parent / "requirements.txt",
            target_runtime=lambda_.Runtime.PYTHON_3_12,
            target_architecture=lambda_.Architecture.ARM_64,   # or X86_64
        )
        layer = builder.as_layer_version(self, "DepsLayer")

        lambda_.Function(
            self, "Fn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("./my_handler"),
            layers=[layer],
            timeout=Duration.seconds(15),
        )
```

That's it — `cdk deploy`, and the consumer Function imports its dependencies on the next invocation.

## Architecture support

Pass `target_architecture=lambda_.Architecture.ARM_64` for Graviton consumers. The construct:

- runs the build Lambda on the chosen architecture, so `pip` picks `aarch64` wheels for compiled extensions like `pydantic-core`, `cryptography`, `numpy`;
- sets `compatible_architectures=[chosen]` on the LayerVersion so Lambda rejects mismatched consumers at attach time (instead of a confusing runtime `ImportError`);
- defaults `output_key` to `deps-{ARCH}.zip` so two builders sharing one bucket don't overwrite each other.

The example stack builds for both x86_64 and arm64 off a shared bucket — see `lambda_deps_builder/example/example_stack.py`.

## Why no Node.js variant?

`aws_cdk.aws_lambda_nodejs.NodejsFunction` already handles the JS case adequately:

- it uses **esbuild** to bundle handler code + tree-shaken deps into a single asset;
- if esbuild is on the dev machine, the entire bundle (including `npm install` of `bundling.nodeModules`) runs **locally** with no Docker;
- if esbuild is missing, it transparently falls back to bundling **inside a local Docker container** based on the Node.js Lambda image;
- JS deps are overwhelmingly pure JS, so platform mismatch is rarely an issue. For the rare native module (`sharp`), the Docker fallback handles it.

So the JS pain point this POC would address (Linux-native compiled deps) is largely absent in Node.js and well-handled by `NodejsFunction` for the few cases where it matters. **Use `NodejsFunction` for JS Lambdas.**

## Running the example

```shell
cd lambda_deps_builder
poetry install
poetry run cdk deploy
# invoke either consumer to confirm `requests` is importable on Lambda:
aws lambda invoke \
  --function-name "$(aws cloudformation describe-stacks \
      --stack-name LambdaDepsBuilderExample \
      --query "Stacks[0].Outputs[?OutputKey=='ConsumerX86FunctionName'].OutputValue" \
      --output text)" \
  /tmp/out.json && cat /tmp/out.json
poetry run cdk destroy
```

## Running tests

```shell
poetry install
poetry run pytest -v               # fast tests: synth + handler unit tests, no AWS
poetry run pytest -v -m e2e        # the real-AWS E2E test (deploys, invokes, destroys)
poetry run pytest -v -m ''         # everything (default suite + e2e)
```

`pyproject.toml` sets `addopts = -m 'not e2e'` so a plain `pytest` skips the E2E test by default.

The default suite is pure-Python (CDK assertion templates + mocked handler) — no AWS, no Docker.

The E2E test (`tests/test_e2e_deploy.py`):

- requires the `cdk` CLI on `PATH`, AWS credentials, and the target account/region bootstrapped (`cdk bootstrap` once);
- deploys a uniquely-named stack via `cdk deploy --require-approval never`;
- invokes both consumer Lambdas (x86_64 and arm64) and asserts each returns `{"status": 200, ...}` — proving `requests` was built on Linux Lambda and is importable from a separate consumer Lambda;
- destroys the stack in a `finally` block so a failed deploy or assertion still cleans up;
- takes ~2–3 minutes per run.

## Caveats

- **Lambda layer size limit (250 MB unzipped)** — `pandas` + `numpy` together exceed this. For large dep sets, use a container image Lambda instead.
- **`/tmp` size** — defaults to 0.5 GiB on Lambda; raise `ephemeral_storage_gib` for big trees.
- **First-deploy latency** — the trigger invocation adds ~30–60 s to the first deploy (and to any deploy where requirements changed).
- **Cost is negligible** — a one-shot Lambda invocation per deploy and a tiny S3 object.
- **Two architectures = two builders** — cheap, but the example shows the pattern explicitly.
