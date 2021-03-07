import { handleHTTPError } from "@utils/fetch";
import fromentries from "fromentries";
import fetch from "node-fetch";

import { Vault } from "../../interfaces/vaults";

const YveCRVJar = {
  token: {
    name: "SushiSwap LP Token",
    symbol: "SLP",
    address: "0x5Eff6d166D66BacBC1BF52E2C54dD391AE6b1f48",
    displayAddress: "0x10B47177E92Ef9D5C6059055d92DdF6290848991",
    decimals: 18,
  },
  symbol: "pSLP",
  apy: {
    recommended: 0,
    composite: true,
    description: "Curve Staking Rewards",
    type: "curve",
    data: {},
  },
  address: "0xbD17B1ce622d73bD438b9E658acA5996dc394b0d",
  displayAddress: "0x5Eff6d166D66BacBC1BF52E2C54dD391AE6b1f48",
  strategies: [],
  name: "pickling SushiSwap LP Token",
  decimals: 18,
  type: "v1",
  fees: {
    general: { performanceFee: 0, withdrawalFee: 0 },
    special: {},
  },
  tags: ["picklejar"],
  endorsed: true,
};

const PickleDataUrl =
  "https://api.pickle-jar.info/protocol/jar/yvecrv-eth/performance";

const PickleJarId = "yvecrv-eth";
const PickleTvlUrl = "https://api.pickle-jar.info/protocol/value";

export async function yveCRVJar(): Promise<Vault> {
  let data = await fetch(PickleDataUrl)
    .then(handleHTTPError)
    .then((res) => res.json())
    .catch(() => null);

  const vault = JSON.parse(JSON.stringify(YveCRVJar));
  if (vault.apy) {
    if (data && data.sevenDay) {
      data = fromentries(
        Object.entries(data).map(([key, val]: [string, number]) => [
          key,
          val / 100,
        ])
      );
      vault.apy.recommended = data.sevenDayFarm;
      vault.apy.data = data;
    } else {
      vault.apy.type = "error";
      vault.apy.description = "http error";
    }
  }

  const assets = await await fetch(PickleTvlUrl)
    .then(handleHTTPError)
    .then((res) => res.json())
    .catch(() => null);

  if (assets) {
    const tvl = assets[PickleJarId];
    vault.tvl = {};
    vault.tvl.value = tvl;
  }

  return vault;
}
