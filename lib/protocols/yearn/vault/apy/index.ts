import { Context } from "@data/context";
import { Apy } from "@protocols/common/apy";

import { Vault } from "../interfaces";
import * as v1 from "./v1";
import * as v2 from "./v2";

export { v1, v2 };

export async function calculateApy(vault: Vault, ctx: Context): Promise<Apy> {
  if (vault.type === "v1") {
    return v1.calculateApy(vault, ctx);
  }
  return await v2.calculateApy(vault, ctx);
}
