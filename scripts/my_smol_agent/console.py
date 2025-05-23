import sys
from typing import Optional
from rich.console import Console as RichConsole
from rich.padding import Padding


console = RichConsole()


class Console:
    @staticmethod
    def welcome(agent_name: Optional[str] = None) -> None:
        console.rule("[bold blue]Programmer")
        console.print("Welcome to programmer.")
        if agent_name:
            console.print(f"Using agent: [bold]{agent_name}[/bold]")
        console.print()

    @staticmethod
    def step_start(name: str, color: str) -> None:
        console.rule(f"[bold {color}]Begin {name} step")

    @staticmethod
    def chat_response_start() -> None:
        pass
    
    @staticmethod
    def chat_message_content_delta(message_content_delta: str) -> None:
        "print one token at a time when streaming"
        console.print(f"[bold cyan]{message_content_delta}", end="")

    @staticmethod
    def chat_response_complete(agent_response: str) -> None:
        console.print("\n")

    @staticmethod
    def chat_response(agent_response: str) -> None:
        console.print(f"[bold cyan]{agent_response}\n")

    @staticmethod
    def tool_call_start(tool_call: str) -> None:
        console.print(f"[bold yellow]Tool call: [/bold yellow]{tool_call}\n")

    @staticmethod
    def tool_call_complete(tool_response: str) -> None:
        lines = tool_response.split("\n")
        if len(lines) > 4:
            lines = lines[:4]
            lines.append("...")
            tool_response = "\n".join(lines)
        console.print(
            Padding.indent(f"{tool_response}\n", 4),
            no_wrap=True,
            overflow="ellipsis",
        )

    @staticmethod
    def user_prompt(prompt: str) -> None:
        console.print(f"[bold blue]User Prompt:[/bold blue] {prompt}\n")

    @staticmethod
    def user_input_complete(user_input: str) -> None:
        console.print()