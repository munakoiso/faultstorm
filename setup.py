from setuptools import setup, find_packages

setup(
    name="faultstorm",
    version="0.1.0",
    description="Fault-injection testing framework for distributed databases",
    packages=find_packages(include=["faultstorm*"]),
    python_requires=">=3.9",
    install_requires=[
        "psycopg2-binary",
    ],
)
