import { Context } from "@data/context";
import * as quote from "@protocols/quote";
import BigNumber from "bignumber.js";

import { getPool, getUnderlyingCoins, getVirtualPrice } from "./registry";

export async function price(
  lpToken: string,
  to: string,
  ctx: Context
): Promise<BigNumber> {
  const virtualPrice = await getVirtualPrice(lpToken, ctx);
  const coins = await getUnderlyingCoins(lpToken, ctx);

  const addresses = coins.map(quote.aliased);

  if (addresses.length === 0) {
    throw new Error("no underlying_coins are supported by quote");
  }

  let price = new BigNumber(0);
  for (const address of addresses) {
    try {
      price = await quote.price(address, to, ctx);
      if (price.gt(0)) {
        break;
      }
    } catch {
      continue;
    }
  }

  if (price.isEqualTo(0)) {
    throw new Error("oracle price for underlying_coins is zero");
  }

  return virtualPrice.div(10 ** 18).times(price);
}
