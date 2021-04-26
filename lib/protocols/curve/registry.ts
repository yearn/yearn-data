import { CurveRegistryContract__factory } from "@contracts/index";
import { Context } from "@data/context";
import { NullAddress } from "@utils/constants";

export const CurveRegistryAddress =
  "0x7D86446dDb609eD0F5f8684AcF30380a356b2B4c";

export const CurveMetaPoolAddress =
  "0x0959158b6040D32d04c301A72CBFD6b39E21c9AE";

export async function getPool(lpToken: string, ctx: Context): Promise<string> {
  const metapool = CurveRegistryContract__factory.connect(
    CurveMetaPoolAddress,
    ctx.provider
  );
  if ((await metapool.get_underlying_coins(lpToken)) !== [NullAddress]) {
    return lpToken;
  }
  const registry = CurveRegistryContract__factory.connect(
    CurveRegistryAddress,
    ctx.provider
  );
  return await registry.get_pool_from_lp_token(lpToken);
}

export async function getUnderlyingCoins(
  lpToken: string,
  ctx: Context
): Promise<string[]> {
  const pool = await getPool(lpToken, ctx);
  const registry = CurveRegistryContract__factory.connect(
    CurveRegistryAddress,
    ctx.provider
  );

  let coins = await registry.get_underlying_coins(pool);
  if (coins === [NullAddress]) {
    const metapool = CurveRegistryContract__factory.connect(
      CurveMetaPoolAddress,
      ctx.provider
    );
    coins = await metapool.get_underlying_coins(pool);
  }
  return coins.filter((coin) => coin !== NullAddress);
}

export async function hasCurvePool(
  lpToken: string,
  ctx: Context
): Promise<boolean> {
  return (await getPool(lpToken, ctx)) !== NullAddress;
}
