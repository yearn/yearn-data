import { providers } from "ethers";
import plimit from "p-limit";
import { DDBVaultsCache, EtherscanApiKey, Web3ProviderWss } from "settings/env";

import { Context, data, yearn } from "../lib";
import { CachedVault } from "../lib/interfaces/vaults";
import { backscratcher } from "../lib/special/vaults/backscratcher";
import { unix } from "../lib/utils/time";
import excluded from "../static/vaults/excluded.json";
import { batchSet } from "../utils/ddb";
import wrap from "../utils/wrap";

const limit = plimit(2);

// FetchAllVaults with a batch call to all the available addresses for each
// version. Extracting name, symbol, decimals and the token address.
async function fetchAllVaults(ctx: Context): Promise<yearn.vault.Vault[]> {
  let v1Addresses = await yearn.vault.registry.fetchV1Addresses(ctx);
  let v2Addresses = await yearn.vault.registry.fetchV2Addresses(ctx);

  v1Addresses = v1Addresses.filter((address) => !excluded.includes(address));
  v2Addresses = v2Addresses.filter((address) => !excluded.includes(address));

  console.log(
    "Fetching",
    v1Addresses.length,
    "v1 vaults",
    v2Addresses.length,
    "v2 vaults"
  );

  const vaults = await Promise.all(
    v1Addresses
      .map<Promise<yearn.vault.Vault>>((address) =>
        limit(async () => {
          return await yearn.vault.resolver.resolveV1(address, ctx);
        }).catch(() => {
          throw new Error(`Could not fetch v1: ${address}`);
        })
      )
      .concat(
        v2Addresses.map((address) =>
          limit(async () => {
            return await yearn.vault.resolver.resolveV2(address, ctx);
          }).catch(() => {
            throw new Error(`Could not fetch v2 production: ${address}`);
          })
        )
      )
  );

  return vaults;
}

export const handler = wrap(async () => {
  const provider = new providers.WebSocketProvider(
    Web3ProviderWss,
    "homestead"
  );
  const etherscan = EtherscanApiKey;
  const ctx = new Context({ provider, etherscan });

  const vaults = (await fetchAllVaults(ctx)) as CachedVault[];

  console.log("Calculating APY");

  await Promise.all(
    vaults.map((vault) =>
      limit(async () => {
        try {
          vault.apy = await yearn.vault.apy.calculateApy(vault, ctx);
        } catch (err) {
          console.error(vault, err);
          vault.apy = null;
        }
      })
    )
  );

  console.log("Adding custom vaults");
  const backscratcherVault = await backscratcher(ctx);
  vaults.push(backscratcherVault as CachedVault);

  console.log("Calculating TVL");

  await Promise.all(
    vaults.map((vault) =>
      limit(async () => {
        if (vault.type === "v2") {
          try {
            vault.tvl = await yearn.vault.tvl.calculateTvlV2(vault, ctx);
          } catch (err) {
            console.error(vault, err);
            vault.tvl = null;
          }
        } else {
          vault.tvl = null;
        }
      })
    )
  );

  console.log("Injecting assets in all vaults");

  const assets = await data.assets.fetchAssets();
  const aliases = await data.assets.fetchAliases();

  for (const vault of vaults) {
    const alias = aliases[vault.token.address];
    vault.token.displayName = alias ? alias.symbol : vault.token.symbol;
    vault.displayName = vault.token.displayName;
    vault.token.icon = assets[vault.token.address] || null;
    vault.icon = assets[vault.address] || null;
  }

  console.log("Injecting timestamp in all vaults");

  const timestamp = unix();

  for (const vault of vaults) {
    vault.updated = timestamp;
  }

  await batchSet(DDBVaultsCache, vaults);

  return {
    message: "Job executed correctly",
  };
});
