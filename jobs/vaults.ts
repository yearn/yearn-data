import { providers } from "ethers";
import { yveCRVJar } from "lib/special/vaults/yvecrv-jar";
import plimit from "p-limit";
import { DDBVaultsCache, EtherscanApiKey, Web3ProviderWss } from "settings/env";

import { Context, data, yearn } from "../lib";
import { CachedVault, FetchedVault } from "../lib/interfaces/vaults";
import { backscratcher } from "../lib/special/vaults/backscratcher";
import { unix } from "../lib/utils/time";
import excluded from "../static/vaults/excluded.json";
import { batchSet, scan } from "../utils/ddb";
import wrap from "../utils/wrap";

const limit = plimit(4);

function notEmpty<T>(value: T | null | undefined): value is T {
  return value !== null && value !== undefined;
}

// FetchAllVaults with a batch call to all the available addresses for each
// version. Extracting name, symbol, decimals and the token address.
async function fetchAllVaults(ctx: Context): Promise<FetchedVault[]> {
  let addrV1 = await yearn.vault.registry.v1.fetchAddresses(ctx);
  let addrV2Prod = await yearn.vault.registry.v2.fetchAddresses(ctx);
  let addrV2Test = await yearn.vault.registry.v2.fetchAddressesExperimental(
    ctx
  );

  addrV1 = addrV1.filter((address) => !excluded.includes(address));
  addrV2Prod = addrV2Prod.filter((address) => !excluded.includes(address));
  addrV2Test = addrV2Test.filter((address) => !excluded.includes(address));

  console.log(
    "Fetching",
    addrV1.length,
    "v1 vaults",
    addrV2Prod.length,
    "v2 prod vaults",
    addrV2Test.length,
    "v2 test vaults"
  );

  const vaultsV1 = await Promise.all(
    addrV1.map((address) =>
      limit(async () => {
        const vault = await yearn.vault.resolver.v1.resolveVault(address, ctx);
        return vault;
      }).catch((err) => {
        console.error(`Could not fetch v1 (${address})`);
        console.error(err);
        return null;
      })
    )
  ).then((vaults) => vaults.filter(notEmpty));

  const vaultsV2Prod = await Promise.all(
    addrV2Prod.map((address) =>
      limit(async () => {
        const vault = await yearn.vault.resolver.v2.resolveVault(address, ctx);
        return { ...vault, endorsed: true };
      }).catch((err) => {
        console.error(`Could not fetch v2 prod (${address})`);
        console.error(err);
        return null;
      })
    )
  ).then((vaults) => vaults.filter(notEmpty));

  const vaultsV2Test = await Promise.all(
    addrV2Test.map((address) =>
      limit(async () => {
        const vault = await yearn.vault.resolver.v2.resolveVault(address, ctx);
        return { ...vault, endorsed: false };
      }).catch((err) => {
        console.error(`Could not fetch v2 test (${address})`);
        console.error(err);
        return null;
      })
    )
  ).then((vaults) => vaults.filter(notEmpty));

  return [...vaultsV1, ...vaultsV2Prod, ...vaultsV2Test];
}

export const handler = wrap(async () => {
  const provider = new providers.WebSocketProvider(Web3ProviderWss);
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
          console.error(
            `Could not fetch apy for ${vault.name} (${vault.address})`
          );
          console.error(err);
          vault.apy = null;
        }
      })
    )
  );

  console.log("Calculating TVL");

  await Promise.all(
    vaults.map((vault) =>
      limit(async () => {
        try {
          vault.tvl = await yearn.vault.tvl.calculateTvl(vault, ctx);
        } catch (err) {
          console.error(
            `Could not fetch tvl for ${vault.name} (${vault.address})`
          );
          console.error(err);
          vault.tvl = null;
        }
      })
    )
  );

  console.log("Adding custom vaults");
  const backscratcherVault = await backscratcher(ctx);
  vaults.push(backscratcherVault as CachedVault);
  const yveCRVJarVault = await yveCRVJar();
  vaults.push(yveCRVJarVault as CachedVault);

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

  const prev = await scan<CachedVault>(DDBVaultsCache);

  let newVaults = 0,
    oldVaults = 0;
  for (const vault of vaults) {
    const cached = prev.findIndex((other) => other.address === vault.address);
    cached === -1 ? newVaults++ : oldVaults++;
    prev.splice(cached, 1);
  }

  console.log(`Creating ${newVaults} vaults & updating ${oldVaults} vaults`);
  await batchSet(DDBVaultsCache, vaults);

  console.log(`Found ${prev.length} dead vaults. Delete them manually.`);
  for (const vault of prev) {
    console.log(`${vault.displayName} (${vault.address})`);
  }

  return {
    message: "Job executed correctly",
  };
});
