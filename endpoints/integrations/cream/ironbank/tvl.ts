import fetch from "node-fetch";

import wrap from "../../../../utils/wrap";

const CreamApiURL = "https://api.cream.finance/api/v1/crtoken?comptroller=ironbank";

// type CreamAssets = {
//   [underlying_name: string]: {
//     underlying_price: number;
//     cash: { value: number };
//     underlying_name: string;
//     token_address: string;
//   };
// };

type Prices = {
  token_address: string;
  cash: { value: string };
  underlying_price: { value: string };
};

export const handler = wrap(async () => {
  const prices = await fetch(CreamApiURL).then((res) => res.json());

  const pricesForTVL = prices.filter(
    (prices: Prices) => prices.token_address != "0x7589C9E17BCFcE1Ccaa1f921196FDa177F0207Fc"
  ); // removing the token cy3CRV as this is already taken into account in TVL V1 YEARN.

  const tvl = pricesForTVL.reduce(
    (value: number, prices: Prices) =>
      value + parseFloat(prices.cash.value) * parseFloat(prices.underlying_price.value),
    0
  );

  return { tvl };
});
