from setuptools import setup

setup(
    packages=["walkaway_ml", "walkaway_ml.scripts"],
    package_dir={"walkaway_ml": "trainer", "walkaway_ml.scripts": "trainer/scripts"},
)
