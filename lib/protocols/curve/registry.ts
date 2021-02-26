import { CurveRegistryContract__factory } from "@contracts/index";
import { Context } from "@data/context";
import { NullAddress } from "@utils/constants";

export const CurveRegistryAddress =
  "0x7D86446dDb609eD0F5f8684AcF30380a356b2B4c";

export async function hasCurvePool(
  lpToken: string,
  ctx: Context
): Promise<boolean> {
  const registry = CurveRegistryContract__factory.connect(
    CurveRegistryAddress,
    ctx.provider
  );
  const pool = await registry.get_pool_from_lp_token(lpToken);
  return pool !== NullAddress;
}
