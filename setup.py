from setuptools import setup, find_packages

setup(
    name="gencall",
    version="2.2.10",
    description="GenCall - SIP Traffic Generator",
    packages=find_packages(),
    include_package_data=True,
    package_data={
        "gencall": [
            "etc/*.cfg",
            "scenarios/templates/*.xml",
            "media/*",
        ],
    },
    install_requires=[
        "fastapi>=0.104.0",
        "uvicorn[standard]>=0.24.0",
        "sqlalchemy>=2.0.0",
        "pydantic>=2.0.0",
        "httpx>=0.24.0",
        "dpkt>=1.9.8",
    ],
    extras_require={
        "postgresql": ["psycopg2-binary>=2.9.0"],
    },
    entry_points={
        "console_scripts": [
            "gencall=gencall.cli:main",
            "gencall-server=gencall.main:main",
            "gencall-controller=gencall.controller.app:run",
        ],
    },
    python_requires=">=3.10",
)
