import {
  RegistryV2Contract__factory,
  StrategyV2Contract__factory,
  VaultV2Contract__factory,
} from "lib/contracts/index";
import { Context } from "lib/data/context";
import { NullAddress } from "lib/utils/constants";
import { objectAll } from "lib/utils/promise";

import { Strategy, VaultV2 } from "../interfaces";
import { resolveBasic } from "./common";

export async function resolveStrategyV2(
  address: string,
  ctx: Context
): Promise<Strategy> {
  const strategy = StrategyV2Contract__factory.connect(address, ctx.provider);
  const structure = {
    name: strategy.name(),
  };
  const result = await objectAll(structure);
  return {
    ...result,
    address,
  };
}

export async function fetchTagsV2(
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

export async function resolveV2(
  address: string,
  ctx: Context
): Promise<VaultV2> {
  const basic = await resolveBasic(address, ctx);
  const vault = VaultV2Contract__factory.connect(address, ctx.provider);
  const performanceFee = vault
    .performanceFee()
    .catch(() => vault.performanceFee().catch(() => undefined))
    .then((val) => val && val.toNumber());

  const managementFee = vault
    .managementFee()
    .catch(() => vault.managementFee().catch(() => undefined))
    .then((val) => val && val.toNumber());

  const structure = {
    emergencyShutdown: vault.emergencyShutdown(),
    apiVersion: vault.apiVersion(),
    performanceFee,
    managementFee,
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

  const tags = await fetchTagsV2(address, ctx);

  return { ...basic, ...specific, strategies, tags, type: "v2" };
}
