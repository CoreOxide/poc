# pip_requirements_flattener
Recursively resolves requirements of Python packages using pip.

Usage:
 ```shell script
    dependencies_set: Set[str] = generate_deps_set(package_names=['requests', 'packaging'])
