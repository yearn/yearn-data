import {
  RegistryV1Contract__factory,
  StrategyV1Contract__factory,
} from "@contracts/index";
import { Context } from "@data/context";

import { FeesV1, Strategy, VaultV1 } from "../interfaces";
import { RegistryV1 } from "../registry/v1";
import { resolveBasic } from "./common";

export const DefaultStrategyV1Name = "StrategyUnnamedV1";

export interface VaultV1Info {
  controller: string;
  token: string;
  strategy: string;
  isWrapped: boolean;
  isDelegated: boolean;
}

export async function resolveInfoV1(
  address: string,
  ctx: Context
): Promise<VaultV1Info> {
  const registry = RegistryV1Contract__factory.connect(
    RegistryV1,
    ctx.provider
  );
  return await registry.getVaultInfo(address);
}

export async function resolveStrategyV1(
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

export async function resolveFeesV1(
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
  const performanceFee = await strategy
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
  return {
    general: {
      strategistReward,
      performanceFee,
      withdrawalFee,
      treasuryFee,
    },
    special: { keepCrv },
  };
}

export async function resolveV1(
  address: string,
  ctx: Context
): Promise<VaultV1> {
  const basic = await resolveBasic(address, ctx);
  const info = await resolveInfoV1(address, ctx);
  const fees = await resolveFeesV1(info.strategy, ctx);
  const strategy = await resolveStrategyV1(info.strategy, ctx);
  return {
    ...basic,
    fees,
    strategies: [strategy],
    type: "v1",
  };
}
