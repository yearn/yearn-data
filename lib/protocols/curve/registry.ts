import { CurveRegistryContract__factory, CurveSwapContract__factory } from "@contracts/index";
import { Context } from "@data/context";
import { CallOverrides } from "@ethersproject/contracts";
import { BigNumber, toBigNumber } from "@utils/bignumber";
import { NullAddress } from "@utils/constants";

export const CurveRegistryAddress = "0x90E00ACe148ca3b23Ac1bC8C240C2a7Dd9c2d7f5";

export const CurveMetaPoolAddress = "0x0959158b6040D32d04c301A72CBFD6b39E21c9AE";

const Overrides = {
  "0x53a901d48795C58f485cBB38df08FA96a24669D5": {
    name: "reth",
    pool: "0xF9440930043eb3997fc70e1339dBb11F341de7A8",
    coins: ["0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE", "0x9559Aaa82d9649C7A7b220E7c461d2E74c9a3593"],
  },
  "0x43b4FdFD4Ff969587185cDB6f0BD875c5Fc83f8c": {
    name: "alusd",
    pool: "0x43b4FdFD4Ff969587185cDB6f0BD875c5Fc83f8c",
    coins: ["0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "0xBC6DA0FE9aD5f3b0d58160288917AA56653660E9"],
  },
};

export async function getPool(lpToken: string, ctx: Context): Promise<string> {
  if (lpToken in Overrides && Overrides[lpToken].pool) {
    return Overrides[lpToken].pool;
  }
  const metapool = CurveRegistryContract__factory.connect(CurveMetaPoolAddress, ctx.provider);
  const coins = await metapool.get_underlying_coins(lpToken);
  if (coins.some((coin) => coin !== NullAddress)) {
    return lpToken;
  }
  const registry = CurveRegistryContract__factory.connect(CurveRegistryAddress, ctx.provider);
  return await registry.get_pool_from_lp_token(lpToken);
}

export async function getUnderlyingCoins(lpToken: string, ctx: Context): Promise<string[]> {
  if (lpToken in Overrides && Overrides[lpToken].coins) {
    return Overrides[lpToken].coins;
  }

  const pool = await getPool(lpToken, ctx);
  const registry = CurveRegistryContract__factory.connect(CurveRegistryAddress, ctx.provider);

  let coins = await registry.get_underlying_coins(pool);
  if (coins === [NullAddress]) {
    const metapool = CurveRegistryContract__factory.connect(CurveMetaPoolAddress, ctx.provider);
    coins = await metapool.get_underlying_coins(pool);
  }
  return coins;
}

export async function getVirtualPrice(
  lpToken: string,
  ctx: Context,
  overrides: CallOverrides = {}
): Promise<BigNumber> {
  const pool = CurveSwapContract__factory.connect(await getPool(lpToken, ctx), ctx.provider);
  return pool.get_virtual_price(overrides).then(toBigNumber);
}

export async function hasCurvePool(lpToken: string, ctx: Context): Promise<boolean> {
  return (await getPool(lpToken, ctx)) !== NullAddress;
}
