import {
  RegistryV1Contract__factory,
  StrategyV1Contract__factory,
} from "@contracts/index";
import { Context } from "@data/context";

import { FeesV1, Strategy, VaultV1 } from "../interfaces";
import { Registry } from "../registry/v1";
import { resolveBasic } from "./common";

export const DefaultStrategyV1Name = "StrategyUnnamedV1";

export interface VaultV1Info {
  controller: string;
  token: string;
  strategy: string;
  isWrapped: boolean;
  isDelegated: boolean;
}

export async function resolveInfo(
  address: string,
  ctx: Context
): Promise<VaultV1Info> {
  const registry = RegistryV1Contract__factory.connect(Registry, ctx.provider);
  return await registry.getVaultInfo(address);
}

export async function resolveStrategy(
  address: string,
  ctx: Context
): Promise<Strategy> {
  const strategy = StrategyV1Contract__factory.connect(address, ctx.provider);
  try {
    const name = await strategy.getName();
    return { address, name };
  } catch {
    return { address, name: DefaultStrategyV1Name };
  }
}

export async function resolveFees(
  address: string,
  ctx: Context
): Promise<FeesV1> {
  const strategy = StrategyV1Contract__factory.connect(address, ctx.provider);
  const strategistReward = await strategy
    .strategistReward()
    .then((val) => val && val.toNumber())
    .catch(() => 0);
  const treasuryFee = await strategy
    .treasuryFee()
    .then((val) => val && val.toNumber())
    .catch(() => 0);
  const strategyPerformanceFee = await strategy
    .performanceFee()
    .then((val) => val && val.toNumber())
    .catch(() => 0);
  const withdrawalFee = await strategy
    .withdrawalFee()
    .then((val) => val.toNumber())
    .catch(() => 0);
  const keepCrv = await strategy
    .keepCRV()
    .then((val) => val.toNumber())
    .catch(() => 0);

  const performanceFee =
    strategistReward + treasuryFee + strategyPerformanceFee;
  return {
    general: {
      performanceFee,
      withdrawalFee,
    },
    special: { keepCrv },
  };
}

export async function resolveVault(
  address: string,
  ctx: Context
): Promise<VaultV1> {
  const basic = await resolveBasic(address, ctx);
  const info = await resolveInfo(address, ctx);
  const fees = await resolveFees(info.strategy, ctx);
  const strategy = await resolveStrategy(info.strategy, ctx);
  return {
    ...basic,
    fees,
    strategies: [strategy],
    type: "v1",
  };
}
