"""Hörfrau - a simulated Hörmann SupraMatic (BlueSecur BLE) garage door drive."""


def main() -> None:
    from .cli import main as cli_main

    cli_main()
