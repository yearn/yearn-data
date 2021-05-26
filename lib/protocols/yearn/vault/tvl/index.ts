import { Context } from "@data/context";
import { curve, quote } from "@protocols/index";
import BigNumber from "bignumber.js";

import { Vault } from "../interfaces";
import * as v1 from "./v1";
import * as v2 from "./v2";

export { v1, v2 };

export interface Tvl {
  value: string;
  price: number;
  totalAssets: number;
}

export async function calculateTvl(vault: Vault, ctx: Context): Promise<Tvl | null> {
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
    price = await curve.price(vault.token.address, quote.USDC.address, ctx);
  } else {
    try {
      decimals = vault.token.decimals;
      price = await quote.price(vault.token.address, quote.USDC.address, ctx);
    } catch {
      try {
        decimals = vault.decimals;
        price = await quote.price(vault.address, quote.USDC.address, ctx);
      } catch {
        throw new Error("vault is not curve and both vault token and vault as token are not supported by quote");
      }
    }
  }
  return {
    value: totalAssets
      .times(price)
      .div(10 ** (decimals + quote.USDC.decimals))
      .decimalPlaces(quote.USDC.decimals)
      .toString(),
    price: price.div(10 ** quote.USDC.decimals).toNumber(),
    totalAssets: totalAssets.toNumber(),
  };
}
