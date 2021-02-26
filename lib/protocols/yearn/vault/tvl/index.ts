import { Context } from "@data/context";

import { Vault } from "../interfaces";
import * as v1 from "./v1";
import * as v2 from "./v2";

export { v1, v2 };

export async function calculateTvl(
  vault: Vault,
  ctx: Context
): Promise<number> {
  if (vault.type == "v1") {
    return v1.calculateTvl(vault, ctx);
  }
  return v2.calculateTvl(vault, ctx);
}
