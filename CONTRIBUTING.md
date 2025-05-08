# Contributing to Akita-Zmodem-MeshCore

Thank you for considering contributing to Akita-Zmodem-MeshCore! We welcome your help to improve this utility. Your contributions can make this tool more robust, feature-rich, and easier to use for everyone.

## How to Contribute

We encourage contributions in various forms:

* **Reporting Bugs**: If you encounter a bug, please open an issue on the GitHub repository. To help us understand and reproduce the issue, please include:
    * A clear and descriptive title.
    * Detailed steps to reproduce the bug.
    * What you expected to happen and what actually happened.
    * Your operating system, Python version.
    * Versions of key libraries (`meshcore`, `zmodem`, `aiofiles`).
    * Any relevant console output, logs (especially with debug logging if possible), or error messages.
    * Screenshots or screencasts if they help illustrate the problem.

* **Suggesting Enhancements**: If you have an idea for a new feature, an improvement to an existing one, or a change in behavior, please open an issue to discuss it first. This allows us to align on the proposal before significant development work is done.

* **Documentation Improvements**: Clear documentation is crucial. If you find areas in the README, usage guides, or code comments that could be improved, please don't hesitate to suggest changes or submit a pull request.

* **Code Contributions (Pull Requests)**:
    1.  **Fork the Repository**: Start by forking the official `Akita-Zmodem-MeshCore` repository to your own GitHub account.
    2.  **Create a Branch**: Create a new branch in your fork for your feature or bug fix. Use a descriptive name (e.g., `git checkout -b feature/improve-status-reporting` or `fix/timeout-handling-bug`).
    3.  **Make Your Changes**: Write your code, ensuring it adheres to the general style and structure of the existing codebase.
    4.  **Write Clear Code**: Aim for readable and maintainable code. Add comments for complex logic or non-obvious decisions.
    5.  **Test Thoroughly**: Test your changes in a realistic environment, considering different use cases and potential edge cases. If possible, write unit tests for new functionality.
    6.  **Update Documentation**: If your changes affect usage, configuration, or add new features, please update the relevant documentation files (`README.md`, `docs/USAGE.md`, `docs/CONFIGURATION.md`).
    7.  **Commit Your Changes**: Use clear and concise commit messages that explain the "why" behind your changes.
    8.  **Push to Your Fork**: Push your branch to your forked repository on GitHub.
    9.  **Open a Pull Request**: Navigate to the original `Akita-Zmodem-MeshCore` repository and open a pull request from your branch. Provide a clear description of your changes in the pull request.

## Development Setup

1.  Clone your fork: `git clone https://github.com/YOUR_USERNAME/Akita-Zmodem-MeshCore.git`
2.  Navigate to the project directory: `cd Akita-Zmodem-MeshCore`
3.  It's highly recommended to use a Python virtual environment:
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```
4.  Install dependencies, including any development tools:
    ```bash
    pip install -r requirements.txt
    # pip install black flake8 # For linting and formatting (optional, but good practice)
    ```

## Coding Style and Conventions

* **PEP 8**: Please follow the PEP 8 style guide for Python code. Most IDEs can help with this. Using a linter like `flake8` and a formatter like `black` is encouraged.
* **Async/Await**: Use `async` and `await` correctly for non-blocking I/O operations.
* **Error Handling**: Implement robust error handling using `try...except` blocks where appropriate. Log errors effectively.
* **Comments**: Add comments to explain complex sections of code, assumptions made, or the purpose of functions and classes.
* **Modularity**: Keep functions and classes focused on specific tasks.

## Issue Tracker and Communication

* Use the GitHub issue tracker for all bug reports and feature requests.
* Be respectful and constructive in all communications.

Thank you for contributing to Akita-Zmodem-MeshCore!
