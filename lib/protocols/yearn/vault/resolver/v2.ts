import {
  RegistryV2Contract__factory,
  StrategyV2Contract__factory,
  VaultV2Contract__factory,
} from "@contracts/index";
import { Context } from "@data/context";
import { NullAddress } from "@utils/constants";
import { objectAll } from "@utils/promise";

import { FeesV2, SpecialFeesV2, Strategy, VaultV2 } from "../interfaces";
import { resolveBasic } from "./common";

export async function resolveStrategy(
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

export async function resolveTags(
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

export async function resolveFees(
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

  if (strategyAddresses.length !== 1) {
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
    .catch(() => undefined);

  return { general, special: { keepCrv } };
}

export async function resolveSpecialFees(
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

export async function resolveVault(
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

  let i = 0;
  let strategyAddress = await vault.withdrawalQueue(i++);

  while (strategyAddress !== NullAddress) {
    strategyAddresses.push(strategyAddress);
    strategyAddress = await vault.withdrawalQueue(i++);
  }

  const strategies = await Promise.all(
    strategyAddresses.map((address) => resolveStrategy(address, ctx))
  );

  const tags = await resolveTags(address, ctx);

  const fees = await resolveFees(address, strategyAddresses, ctx);

  return { ...basic, ...specific, strategies, tags, fees, type: "v2" };
}
