import { CurveRegistryContract__factory } from "@contracts/index";
import { Context } from "@data/context";
import * as coingecko from "@protocols/coingecko";
import { resolveToken } from "@protocols/common";
import * as uniquote from "@protocols/uniquote";
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

  // FIXME: remove, uniquote doesn't currently support eur
  const eurs = "0xdB25f211AB05b1c97D595516F45794528a807ad8";
  if (coins.includes(eurs) && to === uniquote.USDC.address) {
    const price = await coingecko
      .price(eurs, ["usd"])
      .then((price) => toBigNumber(price.usd));

    const token = await resolveToken(eurs, ctx);
    return virtualPrice
      .div(10 ** 18)
      .times(price.times(10 ** (token.decimals + 4)));
  }

  const addresses = coins.filter(uniquote.supported).map(uniquote.aliased);

  if (addresses.length === 0) {
    console.error(coins);
    throw new Error("no underlying_coins are supported by uniquote");
  }

  let price = new BigNumber(0);
  for (const address of addresses) {
    const token = await resolveToken(address, ctx);
    const amount = new BigNumber(10 ** token.decimals);
    try {
      price = await uniquote.price(address, to, amount, ctx);
      break;
    } catch {
      continue;
    }
  }

  if (price.isEqualTo(0)) {
    throw new Error("no underlying_coins have valid quotes on uniquote");
  }

  return virtualPrice.div(10 ** 18).times(price);
}
