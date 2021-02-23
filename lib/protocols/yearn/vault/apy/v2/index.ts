import { Context } from "@data/context";
import { Apy } from "@protocols/common/apy";
import * as curve from "@protocols/curve";

import { VaultV2 } from "../../interfaces";
import { calculateAveragedApy, shouldBeAveraged } from "./averaged";
import { calculateSimpleApy } from "./simple";

export async function calculateApy(vault: VaultV2, ctx: Context): Promise<Apy> {
  const isCurveVault = await curve.hasCurvePool(vault.token.address, ctx);
  if (isCurveVault) {
    return await curve.calculateApy(vault, ctx);
  } else if (shouldBeAveraged(vault)) {
    return await calculateAveragedApy(vault, ctx);
  }
  return await calculateSimpleApy(vault, ctx);
}

export { calculateAveragedApy, calculateSimpleApy };
