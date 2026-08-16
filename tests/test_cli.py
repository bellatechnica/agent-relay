from agent_relay import cli


def test_none_authentication_mode_does_not_require_admin_token(
    monkeypatch, tmp_path
):
    observed = {}
    monkeypatch.delenv("AGENT_RELAY_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "agent-relay",
            "--authentication-mode",
            "none",
            "--database-path",
            str(tmp_path / "relay.sqlite3"),
        ],
    )

    def observe_run(app, *, host, port):
        observed.update(
            app=app,
            host=host,
            port=port,
        )

    monkeypatch.setattr(cli.uvicorn, "run", observe_run)

    cli.main()

    assert observed["app"].state.authentication_mode == "none"
    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 8787
