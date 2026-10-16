"""Dated Finance contribution checks against the registered upstream routes.

The route data comes from the captured adapter, not the local overlap inventory.
Unknown calls leave a contribution unavailable rather than treating it as zero.
"""


def call(address, signature, types=(), args=(), outputs=('uint256',)):
    return address, signature, list(types), list(args), list(outputs)


def capped_debt(debt, assets, shares, supply, route, parent_asset, child_asset):
    if shares > supply:
        raise ValueError('invalid router ownership')
    owned = assets * shares // supply if supply else 0
    if route.get('parentAsset'):
        if parent_asset != route['parentAsset'] or child_asset != route['childAsset']:
            raise ValueError('converter assets changed')
        owned = owned * 10 ** route['parentDecimals'] // 10 ** route['childDecimals']
    elif parent_asset != child_asset:
        raise ValueError('router assets changed')
    return min(debt, owned)


def contributions(reader, chain, active, balances, debts, policy):
    """Return raw net contributions, preserving unknown route reads as None."""
    result = {}; evidence = []
    addresses = list(active)
    token = reader.call([call(a,'token()',outputs=('address',)) for a in addresses])
    asset = reader.call([call(a,'asset()',outputs=('address',)) for a in addresses])
    tokens = {a:(x or y)[0].lower() if x or y else None for a,x,y in zip(addresses,token,asset)}
    counted = {a for a in addresses if tokens[a]}
    for parent in addresses:
        if not tokens[parent] or debts.get(parent) is None:
            result[parent] = None
            continue
        if tokens[parent] != parent and tokens[parent] in counted:
            result[parent] = 0
            evidence.append(dict(parent=parent,child=tokens[parent],method='vault-asset',deduction=str(balances[parent][0])))
            continue
        total = 0
        for strategy,debt in debts[parent]:
            key=(chain,parent,strategy); child=policy['cross'].get(key,strategy)
            if child not in active:continue
            route=policy.get('routes',{}).get(key,{})
            pa=(active[parent].get('asset') or {}).get('address','').lower()
            ca=(active[child].get('asset') or {}).get('address','').lower()
            if pa != ca and key not in policy['cross']:continue
            if route.get('getter'):
                values=reader.call([call(strategy,route['getter']+'()',outputs=('address',)),
                    call(child,'balanceOf(address)',('address',),(strategy,)),call(child,'totalSupply()')])
                if any(v is None for v in values):
                    total=None;break
                destination,shares,supply=(v[0] for v in values)
                if destination.lower()!=child:raise ValueError('router destination changed')
                debt=capped_debt(debt,balances[child][0],shares,supply,route,pa,ca)
            total+=debt
            evidence.append(dict(parent=parent,strategy=strategy,child=child,method='registered' if route else 'direct',deduction=str(debt)))
        result[parent]=max(0,balances[parent][0]-total) if total is not None else None
    if chain == 1:
        parent='0x696d02db93291651ed510704c9b286841d506987'
        sender='0xc5b16e7efe1ca05714477b8edcab4dee9b93a27c'
        if parent in active:
            from .analytics_collect import Reader, fetch
            from .tvl_sources import ArchiveReader
            holder='0xefeebaacdc5640a123fe282652503ea4fd499d14'
            child='0x80c34bd3a3569e126e7055831036aa7b212cb159'
            target=sender if sender in active else parent
            try:
                inventory=fetch('https://kong.yearn.fi/api/rest/list/vaults/747474?origin=yearn').json()
                if any(v['address'].lower()==child and v['chainId']==747474 for v in inventory):
                    remote=Reader(747474)
                    archive=ArchiveReader(747474)
                    remote.block=remote.w3.eth.get_block(archive.block_at(int(reader.block['timestamp'])))
                    local=reader.call([call(sender,'REMOTE_CHAIN_ID()'),call(sender,'REMOTE_COUNTERPART()',outputs=('address',)),
                        call(parent,'asset()',outputs=('address',)),
                        call(parent,'strategies(address)',('address',),(sender,),('uint256',)*4)])
                    values=remote.call([call(holder,'REMOTE_COUNTERPART()',outputs=('address',)),
                        call(holder,'vault()',outputs=('address',)),call(child,'asset()',outputs=('address',)),
                        call(child,'balanceOf(address)',('address',),(holder,)),call(child,'totalSupply()'),call(child,'totalAssets()')])
                    if any(v is None for v in local+values):raise ValueError('remote route read unavailable')
                    if (local[0][0]!=747474 or local[1][0].lower()!=holder or
                        local[2][0].lower()!='0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48' or
                        values[0][0].lower()!=sender or values[1][0].lower()!=child or
                        values[2][0].lower()!='0x203a662b0bd271a6ed5a60edfbd04bfce608fd36'):
                        raise ValueError('remote route changed')
                    owed=balances[sender][0] if sender in active else local[3][2]
                    deduction=capped_debt(owed,values[5][0],values[3][0],values[4][0],{},'usdc','usdc')
                    if result.get(target) is not None:result[target]=max(0,result[target]-deduction)
                    evidence.append(dict(parent=target,strategy=sender,child=child,childChainId=747474,
                        method='cross-chain-shares',deduction=str(deduction),remoteBlock=int(remote.block['number'])))
            except Exception:
                result[target]=None
    return result,evidence
