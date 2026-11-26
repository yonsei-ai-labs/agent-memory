from setuptools import find_packages, setup

# Package the canonical schema directory alongside the Python code in wheels and sdists.
setup(packages=[*find_packages("src"), "agent_memory_eval.schemas"])
