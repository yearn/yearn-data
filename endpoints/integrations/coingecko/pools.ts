import BigNumber from "bignumber.js";

import { CachedVault } from "../../../lib/interfaces/vaults";
import { DDBVaultsCache } from "../../../settings/env";
import { scan } from "../../../utils/ddb";
import wrap, { handlerPath } from "../../../utils/wrap";

function filter(vault: CachedVault) {
  return (
    vault.apy &&
    vault.apy.data &&
    vault.apy.recommended &&
    vault.type === "v2" &&
    vault.endorsed &&
    vault.tvl &&
    !vault.emergencyShutdown
  );
}

export const handler = wrap(async () => {
  const cached = await scan<CachedVault>(DDBVaultsCache);

  const vaults = cached.filter(filter);

  return vaults.map((vault) => ({
    identifier: vault.displayName ?? vault.name,
    apy:
      vault.apy &&
      vault.apy.data &&
      vault.apy.recommended &&
      Number(vault.apy.recommended) * 100,
    liquidity_locked: new BigNumber(vault.tvl ? vault.tvl.value : 0).toNumber(),
  }));
});

export default {
  handler: handlerPath(__dirname, "pools.handler"),
  events: [
    {
      http: {
        path: "integrations/coingecko/pools",
        method: "get",
        caching: {
          enabled: true,
        },
      },
    },
  ],
};
