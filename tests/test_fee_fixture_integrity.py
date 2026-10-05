"""Integrity of the selected regression inputs, not historical coverage certification."""
import hashlib
import json
from pathlib import Path


def test_selected_observed_fixture_hashes_and_metadata():
    root = Path(__file__).parent / 'fixtures/fees'
    manifest = json.loads((root / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
    inventory = json.loads((root / 'inventory.json').read_text())
    keys = {(v['chain_id'], v['address']) for v in inventory}
    assert len(keys) == len(inventory)
    for case in json.loads((root / 'cases.json').read_text()):
        event = case['event']
        assert (event['chain_id'], event['vault_address']) in keys
