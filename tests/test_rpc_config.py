from unittest.mock import Mock
import pytest
from yearn_data.config import get_rpc_url, get_rpc_url_by_chain_id
from yearn_data.tvl_sources import ArchiveReader


def clear_rpc(monkeypatch,chain):
 for name in [f'ARCHIVE_RPC_URI_FOR_{chain}',f'ARCHIVE_RPC_URL_{chain}',f'RPC_URI_FOR_{chain}','ETH_RPC_URL']:
  monkeypatch.delenv(name,raising=False)


def test_archive_endpoint_serves_normal_full_node(monkeypatch):
 clear_rpc(monkeypatch,1)
 monkeypatch.setenv('ARCHIVE_RPC_URL_1','https://archive.invalid')
 assert get_rpc_url('eth')=='https://archive.invalid'
 assert get_rpc_url_by_chain_id(1)==get_rpc_url('eth')


def test_explicit_archive_override_beats_named_rpc(monkeypatch):
 clear_rpc(monkeypatch,1)
 monkeypatch.setenv('ETH_RPC_URL','https://normal.invalid')
 monkeypatch.setenv('ARCHIVE_RPC_URL_1','https://archive.invalid')
 assert get_rpc_url('ethereum')=='https://archive.invalid'


def test_empty_override_preserves_named_fallback(monkeypatch):
 clear_rpc(monkeypatch,1)
 monkeypatch.setenv('ARCHIVE_RPC_URL_1','')
 monkeypatch.setenv('ETH_RPC_URL','https://normal.invalid')
 assert get_rpc_url('eth')=='https://normal.invalid'


def test_numeric_catalog_chain_does_not_need_fee_chain_registration(monkeypatch):
 clear_rpc(monkeypatch,999)
 monkeypatch.setenv('ARCHIVE_RPC_URL_999','https://hyperevm.invalid')
 assert get_rpc_url_by_chain_id(999)=='https://hyperevm.invalid'


def test_missing_endpoint_error_has_setting_name(monkeypatch):
 clear_rpc(monkeypatch,999)
 with pytest.raises(ValueError,match='ARCHIVE_RPC_URL_999'):get_rpc_url_by_chain_id(999)


def test_archive_reader_rejects_wrong_chain_before_sampling(monkeypatch):
 clear_rpc(monkeypatch,1)
 monkeypatch.setenv('ARCHIVE_RPC_URL_1','https://wrong.invalid')
 w3=Mock();w3.eth.chain_id=999
 cls=Mock(return_value=w3);monkeypatch.setattr('yearn_data.tvl_sources.Web3',cls)
 with pytest.raises(ValueError,match='RPC chain mismatch'):ArchiveReader(1)
 w3.eth.get_block.assert_not_called()


@pytest.mark.parametrize('chain,tag',[(250,'latest'),(1,'finalized')])
def test_head_policy_respects_chain_consensus(monkeypatch,chain,tag):
 clear_rpc(monkeypatch,chain)
 monkeypatch.setenv(f'ARCHIVE_RPC_URL_{chain}','https://rpc.invalid')
 w3=Mock();w3.eth.chain_id=chain
 w3.eth.get_block.return_value={'number':123,'timestamp':1000}
 monkeypatch.setattr('yearn_data.tvl_sources.Web3',Mock(return_value=w3))
 reader=ArchiveReader(chain)
 w3.eth.get_block.assert_called_once_with(tag)
 assert reader.finality_policy==('opera-bft' if chain==250 else 'rpc-finalized')


@pytest.mark.parametrize('chain,namespace',[(100,'gnosis'),(146,'sonic'),(80094,'berachain'),(4663,'robinhood')])
def test_catalog_prices_use_canonical_namespaces(chain,namespace):
 from yearn_data.pricing import yearn_prices_token_key
 token='0x'+'12'*20
 assert yearn_prices_token_key(chain,token)==namespace+':'+token
