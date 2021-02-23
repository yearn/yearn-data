import {
  RegistryV2Contract__factory,
  StrategyV2Contract__factory,
  VaultV2Contract__factory,
} from "@contracts/index";
import { Context } from "@data/context";
import { NullAddress } from "@utils/constants";
import { objectAll } from "@utils/promise";

import {
  FeesV2,
  GeneralFeesV2,
  SpecialFeesV2,
  Strategy,
  VaultV2,
} from "../interfaces";
import { resolveBasic } from "./common";

export async function resolveStrategyV2(
  address: string,
  ctx: Context
): Promise<Strategy> {
  const strategy = StrategyV2Contract__factory.connect(address, ctx.provider);
  const name = await strategy.name();
  return {
    name,
    address,
  };
}

export async function resolveTagsV2(
  address: string,
  ctx: Context
): Promise<string[]> {
  const registry = RegistryV2Contract__factory.connect(address, ctx.provider);
  const tagFilter = registry.filters.VaultTagged(null, null);
  const tags = await registry.queryFilter(tagFilter);
  return tags
    .filter((event) => event.args && event.args.vault === address)
    .map((event) => event.args && event.args.tag);
}

export async function resolveFeesV2(
  address: string,
  strategyAddresses: string[],
  ctx: Context
): Promise<FeesV2> {
  const vault = VaultV2Contract__factory.connect(address, ctx.provider);
  const performanceFee = await vault
    .performanceFee()
    .then((val) => val && val.toNumber())
    .catch(() => 0);

  const managementFee = await vault
    .managementFee()
    .then((val) => val && val.toNumber())
    .catch(() => performanceFee);

  const general = { performanceFee, managementFee };

  if (strategyAddresses.length > 1) {
    return { general, special: {} };
  }

  const [strategyAddress] = strategyAddresses;
  const strategy = StrategyV2Contract__factory.connect(
    strategyAddress,
    ctx.provider
  );
  const keepCrv = await strategy
    .keepCRV()
    .then((val) => val && val.toNumber())
    .catch(() => 0);

  return { general, special: { keepCrv } };
}

export async function resolveSpecialFeesV2(
  strategyAddresses: string[],
  ctx: Context
): Promise<SpecialFeesV2> {
  if (strategyAddresses.length === 0) {
    const [address] = strategyAddresses;
    const strategy = StrategyV2Contract__factory.connect(address, ctx.provider);
    const keepCrv = await strategy
      .keepCRV()
      .then((val) => val && val.toNumber())
      .catch(() => 0);

    return { keepCrv };
  }
  return {};
}

export async function resolveV2(
  address: string,
  ctx: Context
): Promise<VaultV2> {
  const basic = await resolveBasic(address, ctx);
  const vault = VaultV2Contract__factory.connect(address, ctx.provider);

  const structure = {
    emergencyShutdown: vault.emergencyShutdown(),
    apiVersion: vault.apiVersion(),
  };

  const specific = await objectAll(structure);

  const strategyAddresses: string[] = [];

  let strategyAddress: string,
    i = 0;
  do {
    strategyAddress = await vault.withdrawalQueue(i++);
    strategyAddresses.push(strategyAddress);
  } while (strategyAddress !== NullAddress);

  strategyAddresses.pop(); // Remove NullAddresses

  const strategies = await Promise.all(
    strategyAddresses.map((address) => resolveStrategyV2(address, ctx))
  );

  const tags = await resolveTagsV2(address, ctx);

  const fees = await resolveFeesV2(address, strategyAddresses, ctx);

  return { ...basic, ...specific, strategies, tags, fees, type: "v2" };
}
