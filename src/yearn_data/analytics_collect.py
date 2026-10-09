"""Owned external references, membership evidence and current declared fee metadata."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import re
import time

import requests
from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from web3 import Web3

from .analytics import SCHEMA, put_input, selected_input
from .config import get_rpc_url_by_chain_id
from .multicall import MULTICALL3, MULTICALL3_ABI, selector
from .storage import connect

ADAPTER_URL='https://raw.githubusercontent.com/DefiLlama/DefiLlama-Adapters/main/projects/yearn/index.js'
FACTORIES_URL='https://raw.githubusercontent.com/DefiLlama/DefiLlama-Adapters/main/projects/helper/curators/configs.js'
CURATION_URL='https://raw.githubusercontent.com/DefiLlama/DefiLlama-Adapters/cf9693cdabf9b816e85e6c8ad19cd4c29190d395/projects/yearn-curating/index.js'
FINANCE_CHAINS={1:'ethereum',10:'optimism',137:'polygon',250:'fantom',8453:'base',42161:'arbitrum',747474:'katana'}
CURATION_CHAINS={1:'ethereum',8453:'base',747474:'katana',42161:'arbitrum',999:'hyperliquid'}


def fetch(url):
    r=requests.get(url,timeout=45);r.raise_for_status();return r


def js_array(source,name):
    match=re.search(r'\b(?:const|let)\s+'+re.escape(name)+r'\s*=\s*\[([\s\S]*?)\]',source)
    if not match:raise ValueError('unsupported adapter: missing '+name)
    return set(a.lower() for a in re.findall(r'0x[0-9a-fA-F]{40}',match[1]))


def rules(source,curation):
    # Fail closed when the supported accounting contract is no longer present.
    for marker in ('buildNestedDebtByVault','subtractNestedDebt','get_default_queue','withdrawalQueue','list/vaults/${api.chainId}?origin=yearn'):
        if marker not in source:raise ValueError('unsupported finance adapter: '+marker)
    declared=re.search(r"const chains = \[([^]]+)\]",source)
    if not declared or set(re.findall(r"'([^']+)'",declared[1]))!=set(FINANCE_CHAINS.values()):
        raise ValueError('unsupported finance adapter chain scope')
    cross={}
    for chain,parent,strategy,child in re.findall(r"chainId:\s*(\d+),\s*parent:\s*'(0x\w+)',\s*strategy:\s*'(0x\w+)',\s*child:\s*'(0x\w+)'",source):
        cross[(int(chain),parent.lower(),strategy.lower())]=child.lower()
    curation=re.sub(r'//[^\n]*','',curation)
    owners={};explicit={}
    for chain,name in CURATION_CHAINS.items():
        block=re.search(r'\b'+name+r':\s*\{([\s\S]*?)\n\s*\}',curation)
        text=block[1] if block else ''
        owner=re.search(r'morphoVaultOwners:\s*\[([\s\S]*?)\]',text)
        vaults=re.search(r'turtleclub_erc4626:\s*\[([\s\S]*?)\]',text)
        owners[chain]=set(a.lower() for a in re.findall(r'0x[0-9a-fA-F]{40}',owner[1] if owner else ''))
        explicit[chain]=set(a.lower() for a in re.findall(r'0x[0-9a-fA-F]{40}',vaults[1] if vaults else ''))
    return {'v1':js_array(source,'v1Vaults'),'blacklist':js_array(source,'blacklist')|js_array(source,'v1Vaults'),
            'cross':cross,'owners':owners,'explicit':explicit}


class Reader:
    def __init__(self,chain):
        self.w3=Web3(Web3.HTTPProvider(get_rpc_url_by_chain_id(chain).split(',')[0],request_kwargs={'timeout':30}))
        if chain!=1:
            from web3.middleware import ExtraDataToPOAMiddleware
            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware,layer=0)
        if self.w3.eth.chain_id!=chain:raise ValueError('RPC chain mismatch')
        self.block=self.w3.eth.get_block('latest' if chain==250 else 'finalized')
        self.contract=self.w3.eth.contract(address=Web3.to_checksum_address(MULTICALL3),abi=MULTICALL3_ABI)
    def call(self,calls):
        results=[]
        for start in range(0,len(calls),100):
            batch=calls[start:start+100]
            raw=self.contract.functions.aggregate3([(Web3.to_checksum_address(address),True,selector(signature)+encode(types,args))
                for address,signature,types,args,output in batch]).call(block_identifier=self.block['number'])
            for (ok,data),entry in zip(raw,batch):
                try:results.append(decode(entry[4],data) if ok and data else None)
                except (ValueError,OverflowError,DecodingError):results.append(None)
        return results



def curator_factories(source):
    factories=[]
    for chain,name in CURATION_CHAINS.items():
        block=re.search(r'\n  '+name+r': \{([\s\S]*?)(?=\n  [a-z_]+: \{|\n\})',source)
        if not block:continue
        for array,family in [('vaultFactories','morpho-v1'),('vaultFactoriesV2','morpho-v2')]:
            entries=re.search(array+r':\s*\[([^]]*)\]',block[1])
            if not entries:continue
            for address,start in re.findall(r"address:\s*'(0x[0-9a-fA-F]{40})',\s*fromBlock:\s*(\d+)",entries[1]):
                factories.append({'chain_id':chain,'address':address,'family':family,'from_block':int(start)})
    return factories


def creation_owner(chain,row,factories):
    from .tvl_discovery import DiscoveryReader, CURATION_REGISTRY, FACTORY_EVENTS
    reader=DiscoveryReader(chain)
    number=row.get('deployment_block') or reader.deployment_block(row['address'])
    family=row.get('version')
    if family not in FACTORY_EVENTS:return None
    for factory in factories:
        if factory['chain_id']!=chain or factory['family']!=family:continue
        try:logs=reader.logs(factory['address'],FACTORY_EVENTS[family],number,number)
        except Exception:continue
        for log in logs:
            args=log['args'];address=args.get('metaMorpho',args.get('newVaultV2'))
            if address and address.lower()==row['address'].lower():
                return {'owner':args.get('initialOwner',args.get('owner')),'factory':factory['address'],
                        'blockNumber':number,'transactionHash':Web3.to_hex(log['transactionHash']),
                        'logIndex':int(log['logIndex']),'event':FACTORY_EVENTS[family]['name']}
    return None


def membership_chain(chain,local,candidates,policy,creation):
    observations=[]
    reader=Reader(chain)
    addresses=[r['address'].lower() for r in local]
    finance={r['address'].lower():r for r in candidates if r.get('chainId')==chain and r['address'].lower() not in policy['blacklist']}
    calls=[(a,'totalAssets()',[],[],['uint256']) for a in finance]
    balances=dict(zip(finance,reader.call(calls)))
    active={a:r for a,r in finance.items() if balances[a] and balances[a][0]>0}
    debts=default_debts(reader,active)
    def finance_included(address):
        if chain not in FINANCE_CHAINS:return False,'chain outside finance adapter'
        if chain==1 and address in policy['v1']:
            supply,pps=reader.call([(address,'totalSupply()',[],[],['uint256']),(address,'getPricePerFullShare()',[],[],['uint256'])])
            return (supply[0]*pps[0]>0 if supply and pps else None),'v1 onchain supply and PPS'
        if address not in finance:return False,'not in adapter Kong candidates or explicitly excluded'
        balance=balances[address]
        if balance is None:return None,'totalAssets read unavailable'
        if balance[0]==0:return False,'zero assets'
        if debts.get(address) is None:return None,'strategy queue/debt read unavailable'
        parent_asset=(finance[address].get('asset') or {}).get('address','').lower()
        if not parent_asset:return None,'parent asset unavailable'
        nested=0
        for strategy,debt in debts[address]:
            child=policy['cross'].get((chain,address,strategy),strategy)
            target=active.get(child)
            if not target:continue
            same=(target.get('asset') or {}).get('address','').lower()==parent_asset
            if same or (chain,address,strategy) in policy['cross']:nested+=debt
        return balance[0]>nested,'adapter raw assets minus nested debt'
    for row,address in zip(local,addresses):
        finance_state,reason=finance_included(address)
        curation_state=False
        if chain in CURATION_CHAINS:
            if address in policy['explicit'][chain]:curation_state=True
            elif row.get('tvl_category')=='curation' or row.get('version','').startswith('morpho'):
                event=creation.get((chain,address))
                if event is None:
                    try:event=creation_owner(chain,row,policy['factories'])
                    except Exception:pass
                    if event:creation[(chain,address)]=event
                curation_state=(event['owner'].lower() in policy['owners'][chain]) if event and event.get('owner') else None
                if curation_state is None:reason='curation initial-owner evidence unavailable'
        state=True if finance_state is True or curation_state is True else None if finance_state is None or curation_state is None else False
        observations.append({'chainId':chain,'address':address,'included':state,'reason':reason,
                             'evidence':{'candidate':address in finance,'assetBalance':str(balances[address][0]) if balances.get(address) else None,'strategyDebts':debts.get(address),'creation':creation.get((chain,address)), 'curationExplicit':address in policy['explicit'].get(chain,set())},
                             'blockNumber':int(reader.block['number']),'blockTimestamp':int(reader.block['timestamp'])})
    return observations


def default_debts(reader,active):
    calls=[];slots=[];result={a:[] for a in active}
    for address,row in active.items():
        if not row.get('strategiesCount'):continue
        v3=str(row.get('apiVersion','')).startswith('3')
        if v3:
            slots.append((address,True));calls.append((address,'get_default_queue()',[],[],['address[]']))
        else:
            for slot in range(min(int(row['strategiesCount']),20)):
                slots.append((address,False));calls.append((address,'withdrawalQueue(uint256)',['uint256'],[slot],['address']))
    debts=[];debt_slots=[]
    for (address,v3),value in zip(slots,reader.call(calls)):
        if value is None:result[address]=None;continue
        queue=value[0] if v3 else [value[0]]
        for strategy in queue:
            strategy=strategy.lower()
            if int(strategy,16)==0:continue
            count=4 if v3 else 9
            debts.append((address,'strategies(address)',['address'],[strategy],['uint256']*count));debt_slots.append((address,strategy,2 if v3 else 6))
    for (address,strategy,index),value in zip(debt_slots,reader.call(debts)):
        if value is None:result[address]=None
        elif result[address] is not None:result[address].append((strategy,int(value[index])))
    return result



def observed_configs(chain, local, declared):
    reader=Reader(chain)
    calls=[]
    for row in local:
        address=row['address']
        for signature,output in [('performanceFee()',['uint256']),('managementFee()',['uint256']),('fee()',['uint256']),('accountant()',['address'])]:
            calls.append((address,signature,[],[],output))
    values=reader.call(calls)
    by_address={r['address']:r for r in declared}
    accountants=[];slots=[]
    for index,row in enumerate(local):
        address=row['address'].lower();perf,mgmt,morpho,accountant=values[index*4:index*4+4]
        config=by_address.get(address,{'chainId':chain,'address':address})
        config.update(declaredPerformanceFee=config.get('performanceFee'),declaredManagementFee=config.get('managementFee'),
                      performanceFee=None,managementFee=None,fixedRateSupported=False,
                      source='onchain-current-fee-config',blockNumber=int(reader.block['number']),observedAt=int(reader.block['timestamp']))
        version=row.get('version','');api=row.get('api_version') or ''
        if version=='morpho-v1' and morpho:
            config.update(performanceFee=morpho[0]/10**14,managementFee=0,fixedRateSupported=True)
        elif version=='morpho-v2' and perf and mgmt:
            config.update(performanceFee=perf[0]/10**14,managementFee=mgmt[0]/10**14,fixedRateSupported=True)
        elif (version=='v2' or api.startswith('0.')) and perf and mgmt:
            config.update(performanceFee=perf[0],managementFee=mgmt[0],fixedRateSupported=True)
        elif accountant:
            config['accountant']=accountant[0].lower()
            if int(accountant[0],16)==0:
                config.update(performanceFee=0,managementFee=0,fixedRateSupported=True)
            else:
                accountants.append((accountant[0],'getVaultConfig(address)',['address'],[address],['uint16']*6+['bool']))
                slots.append(config)
        elif perf and api.startswith('3.'):
            config.update(performanceFee=perf[0],managementFee=0,fixedRateSupported=True)
        config['availability']='available' if config['fixedRateSupported'] else 'unavailable'
        by_address[address]=config
    for config,value in zip(slots,reader.call(accountants)):
        if value:
            config.update(performanceFee=value[1],managementFee=value[0],refundRatio=value[2],maxFee=value[3],
                          maxGain=value[4],maxLoss=value[5],customVaultConfig=value[6],fixedRateSupported=True,availability='available')
    for config in by_address.values():
        if any(config.get(k) is not None and not math_is_rate(config[k]) for k in ('performanceFee','managementFee')):
            config.update(performanceFee=None,managementFee=None,fixedRateSupported=False,availability='unavailable')
    return list(by_address.values())


def collect(database,progress=print):
    now=int(time.time())
    with closing(connect(database)) as conn:
        conn.executescript(SCHEMA)
        catalog=[dict(r) for r in conn.execute('SELECT * FROM tvl_vaults')]
        creation={}
        for observation in selected_input(conn,'membership')['body'].get('vaults',[]):
            if observation.get('evidence',{}).get('creation'):
                creation[(observation['chainId'],observation['address'])]=observation['evidence']['creation']
        for r in conn.execute("SELECT chain_id,vault_address,decoded_json FROM tvl_inventory_events WHERE source_kind='tvl-curation-factory'"):
            creation[(r['chain_id'],r['vault_address'].lower())]=json.loads(r['decoded_json'])
    snapshots=[];failures=[]
    for protocol in ('yearn-finance','yearn-curating'):
        try:
            response=fetch('https://api.llama.fi/protocol/'+protocol);body=response.json()
            latest=body.get('tvl',[])[-1]
            by_chain={name:rows['tvl'][-1]['totalLiquidityUSD'] for name,rows in body.get('chainTvls',{}).items() if not '-' in name and rows.get('tvl')}
            snapshots.append({'protocol':protocol,'timestamp':latest['date'],'total':latest['totalLiquidityUSD'],
                              'byChain':{('HyperEVM' if k=='Hyperliquid L1' else k):v for k,v in by_chain.items()},
                              'byChainTimestamps':{k:v['tvl'][-1]['date'] for k,v in body.get('chainTvls',{}).items() if '-' not in k and v.get('tvl')},'source':'https://api.llama.fi/protocol/'+protocol,'payloadSha256':hashlib.sha256(response.content).hexdigest()})
        except (requests.RequestException,ValueError,KeyError,IndexError):failures.append(protocol)
    with closing(connect(database)) as conn:
        ref_id=put_input(conn,'defillama',{'snapshots':snapshots,'failedProtocols':failures},'partial' if failures else 'available',now) if snapshots else None
    source=fetch(ADAPTER_URL).text;curation=fetch(CURATION_URL).text;factory_source=fetch(FACTORIES_URL).text;policy=rules(source,curation);policy['factories']=curator_factories(factory_source)
    configs=[];membership=[]
    grouped={c:[r for r in catalog if r['chain_id']==c] for c in sorted({r['chain_id'] for r in catalog})}
    def acquire(chain):
        progress(f'Collecting fee metadata and membership for chain {chain}')
        try:
            response=fetch(f'https://kong.yearn.fi/api/rest/list/vaults/{chain}?origin=yearn')
            candidates=response.json()
            if not isinstance(candidates,list):raise ValueError('invalid Kong vault array')
            by_address={r['address'].lower():r for r in candidates}
            config=[]
            for r in grouped[chain]:
                metadata=by_address.get(r['address'].lower(),{});fees=metadata.get('fees') or {}
                perf=fees.get('performanceFee');mgmt=fees.get('managementFee')
                def rate(v):return float(v) if isinstance(v,(int,float)) and math_is_rate(v) else None
                config.append({'chainId':chain,'address':r['address'].lower(),'performanceFee':rate(perf),'managementFee':rate(mgmt),
                    'source':'kong-declared-current-fees','observedAt':now,'apiVersion':metadata.get('apiVersion'),
                    'availability':'available' if rate(perf) is not None and rate(mgmt) is not None else 'unavailable'})
            try:config=observed_configs(chain,grouped[chain],config)
            except Exception:
                for row in config:row.update(performanceFee=None,managementFee=None,fixedRateSupported=False,availability='unavailable')
            try:members=membership_chain(chain,grouped[chain],candidates,policy,creation)
            except Exception:
                members=[{'chainId':chain,'address':r['address'].lower(),'included':None,'reason':'RPC membership acquisition unavailable'} for r in grouped[chain]]
            return config,members
        except (requests.RequestException,ValueError,KeyError):
            return [],[{'chainId':chain,'address':r['address'].lower(),'included':None,'reason':'Kong membership candidates unavailable'} for r in grouped[chain]]
    with ThreadPoolExecutor(max_workers=4) as executor:
        for config,members in executor.map(acquire,grouped):configs.extend(config);membership.extend(members)
    source_info={'financeAdapter':ADAPTER_URL,'financeAdapterSha256':hashlib.sha256(source.encode()).hexdigest(),
                 'curationAdapter':CURATION_URL,'curationAdapterSha256':hashlib.sha256(curation.encode()).hexdigest(),
                 'financeSource':source,'curationSource':curation,'curationFactoriesSource':factory_source,'curationFactoriesUrl':FACTORIES_URL,
                 'curationFactoriesSha256':hashlib.sha256(factory_source.encode()).hexdigest(),
                 'curationMethod':'frozen last retained adapter revision; creation-owner evidence',
                 'universe':'selected local catalogue evaluated against adapter membership rules'}
    with closing(connect(database)) as conn:
        fee_id=put_input(conn,'fee-config',{'vaults':configs,'expectedVaults':len(catalog)},'available' if len(configs)==len(catalog) and all(r['availability']=='available' for r in configs) else 'partial',now)
        membership_id=put_input(conn,'membership',{'vaults':membership,'source':source_info},'available' if all(r['included'] is not None for r in membership) else 'partial',now)
    return {'referenceId':ref_id,'configurationId':fee_id,'membershipId':membership_id,'configuredVaults':sum(r['availability']=='available' for r in configs),
            'verifiedMembershipVaults':sum(r['included'] is not None for r in membership),'expectedVaults':len(catalog)}


def math_is_rate(value):
    import math
    return math.isfinite(value) and 0<=value<=10000
