import { Context, data, protocols } from "@yfi/sdk";
import { providers } from "ethers";
import * as plimit from "p-limit";

import { CachedVault, PartialVaults } from "../interfaces/vaults";
import { backscratcher } from "../special/vaults/backscratcher";
import * as excluded from "../static/vaults/excluded.json";
import { batchSet } from "../utils/ddb";
import unix from "../utils/timestamp";
import wrap from "../utils/wrap";

const limit = plimit(2);

const VaultsCache = process.env.DDB_VAULTS_CACHE!;

// FetchAllVaults with a batch call to all the available addresses for each
// version. Extracting name, symbol, decimals and the token address.
async function fetchAllVaults(ctx: Context): Promise<PartialVaults[]> {
  let v1Addresses = await protocols.yearn.vault.fetchV1Addresses(ctx);
  let v2Addresses = await protocols.yearn.vault.fetchV2Addresses(ctx);
  const v2ExperimentalAddresses = await protocols.yearn.vault.fetchV2ExperimentalAddresses(
    ctx
  );

  v1Addresses = v1Addresses.filter((address) => !excluded.includes(address));
  v2Addresses = v2Addresses.filter((address) => !excluded.includes(address));

  console.log(
    "Fetching",
    v1Addresses.length,
    "v1 vaults",
    v2Addresses.length,
    "v2 vaults",
    v2ExperimentalAddresses.length,
    "v2 experimental vaults"
  );

  const vaults = await Promise.all(
    v1Addresses
      .map<Promise<PartialVaults>>((address) =>
        limit(async () => {
          return await protocols.yearn.vault.resolveV1(address, ctx);
        })
      )
      .concat(
        v2Addresses.map((address) =>
          limit(async () => {
            const vault = await protocols.yearn.vault.resolveV2(address, ctx);
            return { ...vault, endorsed: true };
          })
        )
      )
      .concat(
        v2ExperimentalAddresses.map((address) =>
          limit(async () => {
            const vault = await protocols.yearn.vault.resolveV2(address, ctx);
            return { ...vault, endorsed: false };
          })
        )
      )
  );

  return vaults;
}

export const handler = wrap(async () => {
  const provider = new providers.WebSocketProvider(
    process.env.WEB3_PROVIDER!,
    "homestead"
  );
  const etherscan = process.env.ETHERSCAN_API_KEY;
  const ctx = new Context({ provider, etherscan });

  const vaults = (await fetchAllVaults(ctx)) as CachedVault[];

  console.log("Calculating APY");

  await Promise.all(
    vaults.map((vault) =>
      limit(async () => {
        try {
          vault.apy = await protocols.yearn.vault.calculateApy(vault, ctx);
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
            vault.tvl = await protocols.yearn.vault.calculateTvlV2(vault, ctx);
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

  await batchSet(VaultsCache, vaults);

  return {
    message: "Job executed correctly",
  };
});
