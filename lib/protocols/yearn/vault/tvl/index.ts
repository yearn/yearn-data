import { Context } from "@data/context";
import { curve, uniquote } from "@protocols/index";
import BigNumber from "bignumber.js";

import { Vault } from "../interfaces";
import * as v1 from "./v1";
import * as v2 from "./v2";

export { v1, v2 };

export async function calculateTvl(
  vault: Vault,
  ctx: Context
): Promise<string | null> {
  let totalAssets: BigNumber;
  if (vault.type == "v1") {
    totalAssets = await v1.calculateTotalAssets(vault, ctx);
  } else {
    totalAssets = await v2.calculateTotalAssets(vault, ctx);
  }
  let price: BigNumber;
  let decimals: number;
  const isCurveVault = await curve.hasCurvePool(vault.token.address, ctx);
  if (isCurveVault) {
    decimals = vault.token.decimals;
    price = await curve.price(vault.token.address, uniquote.USDC.address, ctx);
  } else if (uniquote.supported(vault.token.address)) {
    const amount = new BigNumber(10 ** vault.token.decimals);
    decimals = vault.token.decimals;
    price = await uniquote.price(
      vault.token.address,
      uniquote.USDC.address,
      amount,
      ctx
    );
  } else if (uniquote.supported(vault.address)) {
    const amount = new BigNumber(10 ** vault.decimals);
    decimals = vault.decimals;
    price = await uniquote.price(
      vault.address,
      uniquote.USDC.address,
      amount,
      ctx
    );
  } else {
    throw new Error(
      "vault is not curve and both vault token and vault as token are not supported by uniquote"
    );
  }
  return totalAssets
    .times(price)
    .div(10 ** (decimals + uniquote.USDC.decimals))
    .decimalPlaces(uniquote.USDC.decimals)
    .toString();
}
