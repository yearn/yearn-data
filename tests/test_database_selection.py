from yearn_data import cli, storage


def test_loaded_default_can_be_overridden_without_switching_other_database(monkeypatch,tmp_path):
    env=tmp_path/'local.env'
    env.write_text('YEARN_DATA_DB=neon\n')
    monkeypatch.delenv('YEARN_DATA_DB',raising=False)
    selected=[]
    monkeypatch.setattr(cli,'open_db',lambda database:selected.append(database))
    assert cli.main(['--env',str(env),'init-db'])==0
    assert cli.main(['--env',str(env),'--db','staging','init-db'])==0
    assert selected==['neon','staging']


def test_staging_connection_uses_separate_credentials(monkeypatch):
    from yearn_data import postgres
    monkeypatch.setenv('NEON_DB_URL','postgresql://primary.invalid/primary')
    monkeypatch.setenv('STAGING_DB_URL','postgresql://127.0.0.1/staging?sslmode=disable')
    urls=[]
    monkeypatch.setattr(postgres,'Connection',lambda url,**kwargs:urls.append(url))
    storage.connect('staging')
    assert urls==['postgresql://127.0.0.1/staging?sslmode=disable']
    assert storage.database_reference('staging')=='staging'
