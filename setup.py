from setuptools import setup, find_packages

setup(
    name="dude-wheres-my-logs",
    version="0.1.0",
    packages=find_packages(where='src'),
    package_dir={'': 'src'},
    install_requires=[
        'azure-cli',
    ],
    entry_points={
        'console_scripts': [
            'DudeWheresMyLogs=dwml.__main__:main',
        ],
    },
    python_requires='>=3.8',
    author='Your Name',
    author_email='your.email@example.com',
    description='Azure Diagnostic Log Health Checker',
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    url='https://github.com/yourusername/DudeWheresMyLogs',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
    ],
)
