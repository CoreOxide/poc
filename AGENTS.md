# AGENTS.md — Agent & Contributor Guidelines

This document sets mandatory guidelines and verification procedures for AI agents and human contributors working on this repository.

---

## 1. Project Overview & Architecture

- **`lambda_deps_builder`**: A Python AWS CDK construct that builds Lambda dependencies inside an actual AWS Lambda trigger function during `cdk deploy` — avoiding local Docker daemon requirements, cross-architecture wheel compilation issues, and platform mismatches.
- **Build Engine**: Astral `uv` by default (slashing deploy-time build latency by ~10x), with automatic fallback to standard `pip`.
- **Target Architectures**: Supports both `x86_64` and `arm64` (Graviton).

---

## 2. Mandatory Pre-Push Local Verification

> [!IMPORTANT]
> **Never push changes to Git or open pull requests without running tests locally.**
> Continuous Integration (CI) does not run real AWS deployment tests due to credentials isolation. Therefore, local verification against real AWS is required.

Before committing or pushing any changes to remote branches:

### Step 1: Run Fast Unit & Synth Tests
All unit tests and CDK CloudFormation synthesis assertion tests must pass without errors or warnings:
```bash
cd lambda_deps_builder
poetry run pytest -v
```

### Step 2: Run Real AWS Account Tests (Local Only — Do Not Rely on CI)
When modifying [`construct.py`](lambda_deps_builder/lambda_deps_builder/construct.py), [`handler.py`](lambda_deps_builder/lambda_deps_builder/builder_handler/handler.py), or packaging logic:
1. Ensure your AWS credentials and region are configured (via AWS CLI profile, environment variables, or SSO).
2. Ensure your target account and region have been bootstrapped (`npx cdk bootstrap` or `cdk bootstrap`).
3. Run the live E2E deployment suite:
   ```bash
   cd lambda_deps_builder
   poetry run pytest -v -m e2e
   ```
4. This test will:
   - Deploy a uniquely-named CloudFormation stack (`LambdaDepsBuilderE2E-<uuid>`).
   - Trigger the in-Lambda builder on both `x86_64` and `arm64`.
   - Invoke consumer Lambdas to verify that dependencies are importable and functional.
   - Automatically destroy all deployed AWS resources in a `finally` block to prevent leaks.

### Step 3: Verify Package Build & Distribution Integrity
Ensure the package builds cleanly with no distribution check errors:
```bash
cd lambda_deps_builder
poetry run python -m build --sdist --wheel . --outdir dist
poetry run twine check dist/*
```

---

## 3. Branching & Git Conventions

- **Default Remote Branch**: `main` (hosted on `origin`). Note: `master` does not exist on remote; always branch from and target `origin/main`.
- **Feature Branches**: Use descriptive branch prefixes (e.g. `feat/<feature-name>`, `fix/<bug-name>`).
- **Commit Messages**: Follow Conventional Commits format (e.g. `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`).
