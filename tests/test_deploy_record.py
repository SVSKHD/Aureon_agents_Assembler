"""The deploy recorder's CLI contract (scripts/ops/deploy-poll.sh relies on it)."""

from __future__ import annotations

import pytest

from app.workers import deploy_record as dr


def test_start_args_and_details() -> None:
    args = dr.build_parser().parse_args(
        [
            "start",
            "--target",
            "frontend",
            "--sha",
            "703e004",
            "--from",
            "none",
            "--log",
            "/mnt/spasta-data/logs/deploy.log",
        ]
    )
    assert dr.start_details(args) == {
        "target": "frontend",
        "sha": "703e004",
        "from_sha": None,
        "log": "/mnt/spasta-data/logs/deploy.log",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["start", "--target", "db", "--sha", "abcdef1", "--log", "/x.log"],
        ["start", "--target", "backend", "--sha", "main", "--log", "/x.log"],
        ["start", "--target", "backend", "--sha", "abcdef1", "--log", "relative.log"],
        ["start", "--target", "backend", "--sha", "abcdef1", "--log", "/a/../etc/passwd"],
        ["finish", "--run-id", "1", "--status", "cancelled"],
    ],
)
def test_rejects_bad_input(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        dr.build_parser().parse_args(argv)


def test_finish_error_is_one_bounded_line() -> None:
    args = dr.build_parser().parse_args(
        ["finish", "--run-id", "7", "--status", "failed", "--error", "a\nb\t" + "x" * 900]
    )
    err = dr.finish_error(args)
    assert err is not None and "\n" not in err and len(err) == dr.MAX_ERROR
    assert err.startswith("a b ")


def test_finish_without_error() -> None:
    args = dr.build_parser().parse_args(["finish", "--run-id", "7", "--status", "succeeded"])
    assert dr.finish_error(args) is None and args.rolled_back is False
