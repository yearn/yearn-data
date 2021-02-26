import { Context } from "@data/context";
import { Apy } from "@protocols/common/apy";
import * as curve from "@protocols/curve";

import { VaultV1 } from "../../interfaces";
import { calculateSimpleApy } from "./simple";

export async function calculateApy(vault: VaultV1, ctx: Context): Promise<Apy> {
  const isCurveVault = await curve.hasCurvePool(vault.token.address, ctx);
  if (isCurveVault) {
    return await curve.calculateApy(vault, ctx);
  }
  return await calculateSimpleApy(vault, ctx);
}

export { calculateSimpleApy as calculateSimple };
