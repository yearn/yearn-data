import { ethers } from "ethers";
import { Context } from "lib";
import fetch from "node-fetch";

import CurveRewardDistributionAbi from "../../../static/abi/curve.reward.distribution.abi.json";
import CurveSwapAbi from "../../../static/abi/curve.swap.abi.json";
import CurveVotingEscrowAbi from "../../../static/abi/curve.voting.escrow.abi.json";
import veCurveVaultAbi from "../../../static/abi/ve.curve.vault.abi.json";
import { Vault } from "../../interfaces/vaults";

const BackScratcherMetadata: Vault = {
  token: {
    name: "Curve DAO Token",
    symbol: "CRV",
    address: "0xD533a949740bb3306d119CC777fa900bA034cd52",
    decimals: 18,
  },
  symbol: "yveCRV",
  apy: {
    recommended: 0,
    composite: true,
    description: "yveCRV Admin Fees",
    type: "curve",
    data: {},
  },
  address: "0xc5bDdf9843308380375a611c18B50Fb9341f502A",
  strategies: [],
  name: "veCRV-DAO yVault (yveCRV-DAO)",
  decimals: 18,
  type: "v1",
  fees: {
    general: { performanceFee: 0, withdrawalFee: 0 },
    special: {},
  },
};

// 7 day farm = raccomended

export async function backscratcher(ctx: Context): Promise<Vault> {
  const Curve3pool = new ethers.Contract(
    "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7",
    CurveSwapAbi,
    ctx.provider
  );
  const CurveRewardDistribution = new ethers.Contract(
    "0xA464e6DCda8AC41e03616F95f4BC98a13b8922Dc",
    CurveRewardDistributionAbi,
    ctx.provider
  );
  const CurveVotingEscrow = new ethers.Contract(
    "0x5f3b5DfEb7B28CDbD7FAba78963EE202a494e2A2",
    CurveVotingEscrowAbi,
    ctx.provider
  );
  const veCurveVault = new ethers.Contract(
    "0xc5bDdf9843308380375a611c18B50Fb9341f502A",
    veCurveVaultAbi,
    ctx.provider
  );

  const voter = "0xF147b8125d2ef93FB6965Db97D6746952a133934";

  const resp = await fetch(
    "https://api.coingecko.com/api/v3/simple/price?ids=curve-dao-token,vecrv-dao-yvault&vs_currencies=usd"
  );
  const graph = await resp.json();
  const crv_price = graph["curve-dao-token"]["usd"];
  const yvecrv_price = graph["vecrv-dao-yvault"]["usd"];
  const total_vecrv = await CurveVotingEscrow.totalSupply();

  const yearn_vecrv = await CurveVotingEscrow.balanceOf(voter);
  const vault_supply = await veCurveVault.totalSupply();

  const week = 7 * 86400;
  const epoch = Math.floor(Date.now() / 1000 / week) * week - week;
  const tokens_per_week =
    (await CurveRewardDistribution.tokens_per_week(epoch)) / 1e18;
  const virtual_price = (await Curve3pool.get_virtual_price()) / 1e18;
  const apy =
    (tokens_per_week * virtual_price * 52) / ((total_vecrv / 1e18) * crv_price);

  const vault_boost = (yearn_vecrv / vault_supply) * (crv_price / yvecrv_price);

  const data = {
    currentBoost: vault_boost,
    boostedApy: apy * vault_boost,
    totalApy: apy * vault_boost,
    poolApy: apy,
    baseApy: apy,
  };

  const vault = JSON.parse(JSON.stringify(BackScratcherMetadata));
  if (vault.apy) {
    vault.apy.recommended = data.totalApy;
    vault.apy.data = data;
  }

  return vault;
}
