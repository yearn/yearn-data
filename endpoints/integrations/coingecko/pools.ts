import BigNumber from "bignumber.js";
import fetch from "node-fetch";

import { CachedVault } from "../../../lib/interfaces/vaults";
import { DDBVaultsCache } from "../../../settings/env";
import { scan } from "../../../utils/ddb";
import wrap from "../../../utils/wrap";

const CoinGeckoApiURL = "https://api.coingecko.com/api/v3";

type CoinPrices = { [address: string]: { usd: number } }; // CoinGecko ids

function filter(vault: CachedVault) {
  return (
    vault.apy &&
    vault.apy.data &&
    vault.apy.data.oneMonthSample &&
    vault.type === "v2" &&
    vault.endorsed &&
    vault.tvl &&
    !vault.emergencyShutdown
  );
}

export const handler = wrap(async () => {
  const cached = await scan<CachedVault>(DDBVaultsCache);

  let vaults = cached.filter(filter);

  const params = new URLSearchParams();
  params.append(
    "contract_addresses",
    vaults.map((vault) => vault.token.address).join(",")
  );
  params.append("vs_currencies", "usd");
  const url = `${CoinGeckoApiURL}/simple/token_price/ethereum?${params}`;
  const prices: CoinPrices = await fetch(url).then((res) => res.json());

  vaults = vaults.filter(
    (vault) => !!prices[vault.token.address.toLowerCase()]
  );

  return vaults.map((vault) => ({
    identifier: vault.displayName ?? vault.name,
    apy:
      vault.apy &&
      vault.apy.data &&
      vault.apy.data.oneMonthSample &&
      Number(vault.apy.data.oneMonthSample) * 100,
    liquidity_locked: new BigNumber(vault.tvl ?? 0)
      .multipliedBy(prices[vault.token.address.toLowerCase()].usd)
      .toNumber(),
  }));
});
