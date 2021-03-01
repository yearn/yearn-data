import { Context } from "@data/context";
import { coingecko, curve, uniquote } from "@protocols/index";
import { toBigNumber } from "@utils/bignumber";
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

export async function calculateTvl(
  vault: Vault,
  ctx: Context
): Promise<Tvl | null> {
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
    // FIXME: 1inch is not supported by uniquote
    const _1inch = "0x111111111117dC0aa78b770fA6A738034120C302";
    if (vault.token.address === _1inch) {
      decimals = vault.token.decimals;
      price = await coingecko
        .price(_1inch, ["usd"])
        .then((price) => toBigNumber(price.usd))
        .then((price) => price.times(10 ** 6));
    } else {
      throw new Error(
        "vault is not curve and both vault token and vault as token are not supported by uniquote"
      );
    }
  }
  return {
    value: totalAssets
      .times(price)
      .div(10 ** (decimals + uniquote.USDC.decimals))
      .decimalPlaces(uniquote.USDC.decimals)
      .toString(),
    price: price.div(10 ** uniquote.USDC.decimals).toNumber(),
    totalAssets: totalAssets.toNumber(),
  };
}
