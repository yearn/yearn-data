import { CurveRegistryContract__factory } from "@contracts/index";
import { Context } from "@data/context";
import * as quote from "@protocols/quote";
import { toBigNumber } from "@utils/bignumber";
import BigNumber from "bignumber.js";

import { CurveRegistryAddress } from "./registry";

export async function price(
  lpToken: string,
  to: string,
  ctx: Context
): Promise<BigNumber> {
  const registry = CurveRegistryContract__factory.connect(
    CurveRegistryAddress,
    ctx.provider
  );
  const virtualPrice = await registry
    .get_virtual_price_from_lp_token(lpToken)
    .then(toBigNumber);
  const pool = await registry.get_pool_from_lp_token(lpToken);
  const coins = await registry.get_underlying_coins(pool);

  const addresses = coins.map(quote.aliased);

  if (addresses.length === 0) {
    console.error(coins);
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
    throw new Error("no underlying_coins have valid quotes on quote");
  }

  return virtualPrice.div(10 ** 18).times(price);
}
