import os

import aws_cdk as cdk

from lambda_deps_builder.example.example_stack import ExampleStack


def main() -> None:
    app = cdk.App()
    stack_name = os.environ.get("STACK_NAME", "LambdaDepsBuilderExample")
    ExampleStack(app, stack_name)
    app.synth()


if __name__ == "__main__":
    main()
