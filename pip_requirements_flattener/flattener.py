import concurrent.futures
import subprocess
from pathlib import Path
from typing import Set, List


def generate_deps_set(requirements_txt_file: Path = None, package_names: List[str] = None) -> Set[str]:
    """
    Generates a fully resolved, flat requirements list from a requirements.txt file or a package name
    :param package_names: names of python packages you want to resolve dependencies for
    :param requirements_txt_file: a generated or a manually created requirements.txt file.
    :return: A set of recursively resolved dependencies.
    """
    if requirements_txt_file is None and package_names is None:
        raise ValueError('must supply either file Path or package name.')

    if requirements_txt_file:
        with open(requirements_txt_file.as_posix(), 'r', encoding='utf-8') as req_file:
            requirements_list = req_file.readlines()
    else:
        requirements_list = package_names

    dependencies_set: Set[str] = set()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

    package_names = [line.split('==', maxsplit=1)[0] for line in requirements_list]
    futures = [
        executor.submit(_add_dependencies_recursively, package_name=package_name, dependencies_set=dependencies_set)
        for package_name in package_names
    ]

    concurrent.futures.wait(futures)
    executor.shutdown()
    # remove AWS natively-provided dependencies
    dependencies_set.difference_update({'aws-cdk', 'boto3', 'docker', 'botocore'})

    return dependencies_set


def _add_dependencies_recursively(package_name: str, dependencies_set: Set[str]) -> None:
    """
    Updates passed dependencies set with all dependencies from the packages' dependency tree, recursively.
    :param package_name: packages you want to resolve dependencies for.
    :param dependencies_set: string set of dependencies. must be empty on first call.
    """
    print(f'current dependencies set: {str(dependencies_set)}')
    return_value = subprocess.run(['python', '-m', 'pip', 'show', package_name], capture_output=True, check=True)
    print(f'adding package {package_name}')
    dependencies_set.add(package_name)
    text_output: str = return_value.stdout.decode()
    for item in text_output.splitlines():
        if 'Requires' in item:  # if this package has sub-dependencies
            requirements_list: List[str] = item.split(':', maxsplit=1)[1].replace(' ', '').split(',')
            for requirement in requirements_list:
                if not requirement or requirement in dependencies_set:
                    print(f'package already added - {requirement}, skipping.')
                    continue  # package has no requirements, or we have already resolved dependencies for this package
                _add_dependencies_recursively(requirement, dependencies_set)
            break
