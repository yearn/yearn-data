import fetch from "node-fetch";

import wrap from "../../../utils/wrap";

const CreamApiURL = "https://api.cream.finance/api/v1/crtoken?comptroller=ironbank";


type creamAssets = { [underlying_name: string]: { underlying_price: number, cash:{value:number}, underlying_name:string, token_address:string }}; // Cream assets, price, amount


export const handler = wrap(async () => {


  const prices = await fetch(CreamApiURL).then((res) => res.json());
  
  const pricesForTVL = prices.filter(prices => prices.token_address != '0x7589C9E17BCFcE1Ccaa1f921196FDa177F0207Fc') // removing the token cy3CRV as this is already taken into account in TVL V1 YEARN.

  const total:creamAssets = pricesForTVL.reduce((r:number, prices) =>r + parseFloat(prices.cash.value)* parseFloat(prices.underlying_price.value),0);
  return { IronBankTVL:total };
  
});
