from setuptools import find_packages, setup

# Include the canonical schemas and the retail execution modules in editable installs and wheels.
setup(packages=[*find_packages("src"), "agent_memory_eval.schemas", "scripts"])
