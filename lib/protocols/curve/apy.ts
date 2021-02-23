import {
  CurveGaugeContract__factory,
  CurveGaugeControllerContract__factory,
  CurveRegistryContract__factory,
} from "@contracts/index";
import { Context } from "@data/context";
import { getPrice } from "@protocols/coingecko";
import { Apy, calculateFromPps } from "@protocols/common/apy";
import { Vault } from "@protocols/yearn/vault/interfaces";
import { BigNumber, toBigNumber } from "@utils/bignumber";
import { estimateBlockPrecise, fetchLatestBlock } from "@utils/block";
import { seconds } from "@utils/time";

import { getPoolFromLpToken } from "./registry";

const CurveRegistryAddress = "0x7D86446dDb609eD0F5f8684AcF30380a356b2B4c";
const CrvAddress = "0xD533a949740bb3306d119CC777fa900bA034cd52";

const WbtcAddress = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599";
const RenBtcAddress = "0xEB4C2781e4ebA804CE9a9803C67d0893436bB27D";
const SBtcAddress = "0xfE18be6b3Bd88A2D2A7f928d00292E7a9963CfC6";
const SEthAddress = "0x5e74C9036fb86BD7eCdcb084a0673EFc32eA31cb";

const EthAddress = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE";
const StEthAddress = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84";
const WethAddress = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2";
const YearnVeCrvvoterAddress = "0xF147b8125d2ef93FB6965Db97D6746952a133934";

const MaxBoost = 2.5;

export async function calculatePoolApr(
  vault: Vault,
  ctx: Context
): Promise<number | null> {
  const lpToken = vault.token.address;
  const registry = CurveRegistryContract__factory.connect(
    CurveRegistryAddress,
    ctx.provider
  );
  const latest = await fetchLatestBlock(ctx);
  const oneMonth = await estimateBlockPrecise(
    latest.timestamp - seconds("1 day"),
    ctx
  );

  const poolAprSamples = await calculateFromPps(
    latest.block,
    oneMonth,
    { oneMonthSample: oneMonth },
    (overrides) => registry.get_virtual_price_from_lp_token(lpToken, overrides)
  );
  const poolApr = poolAprSamples.oneMonthSample;
  return poolApr;
}

const btcLikeAddresses = [RenBtcAddress, WbtcAddress, SBtcAddress];

const ethLikeAddresses = [SEthAddress, EthAddress, WethAddress, StEthAddress];

export async function calculateApy(vault: Vault, ctx: Context): Promise<Apy> {
  const lpToken = vault.token.address;
  const registry = CurveRegistryContract__factory.connect(
    CurveRegistryAddress,
    ctx.provider
  );
  const poolAddress = await getPoolFromLpToken(lpToken, ctx);
  const gauges = await registry.get_gauges(poolAddress);
  const gaugeAddress = gauges[0][0]; // first gauge

  const gauge = CurveGaugeContract__factory.connect(gaugeAddress, ctx.provider);
  const gaugeControllerAddress = await gauge.controller();
  const gaugeController = CurveGaugeControllerContract__factory.connect(
    gaugeControllerAddress,
    ctx.provider
  );

  const gaugeWorkingSupply = toBigNumber(await gauge.working_supply());
  const gaugeRelativeWeight = toBigNumber(
    await gaugeController.gauge_relative_weight(gaugeAddress)
  );
  const gaugeInflationRate = toBigNumber(await gauge.inflation_rate());
  const poolVirtualPrice = toBigNumber(
    await registry.get_virtual_price_from_lp_token(lpToken)
  );

  const underlyingCoins = await registry.get_underlying_coins(poolAddress);
  const firstUnderlyingCoinAddress = underlyingCoins[0];

  let btcMatch = false;
  let ethMatch = false;
  underlyingCoins.every((tokenAddress) => {
    btcMatch = btcLikeAddresses.includes(tokenAddress);
    ethMatch = ethLikeAddresses.includes(tokenAddress);
    return !(btcMatch || ethMatch);
  });

  let priceOfBaseAsset;
  if (btcMatch) {
    priceOfBaseAsset = await getPrice(WbtcAddress, ["usd"]);
  } else if (ethMatch) {
    priceOfBaseAsset = await getPrice(WethAddress, ["usd"]);
  } else {
    priceOfBaseAsset = await getPrice(firstUnderlyingCoinAddress, ["usd"]);
    priceOfBaseAsset = priceOfBaseAsset || { usd: 1 };
  }

  const priceOfCrv = await getPrice(CrvAddress, ["usd"]);

  const yearnWorkingBalance = toBigNumber(
    await gauge.working_balances(YearnVeCrvvoterAddress)
  );
  const yearnGaugeBalance = toBigNumber(
    await gauge.balanceOf(YearnVeCrvvoterAddress)
  );

  const secondsInYear = new BigNumber(seconds("1 year"));
  const inverseMaxBoost = new BigNumber(1 / MaxBoost);

  const baseApy = gaugeInflationRate
    .times(gaugeRelativeWeight)
    .times(secondsInYear.div(gaugeWorkingSupply))
    .times(inverseMaxBoost.div(poolVirtualPrice))
    .times(priceOfCrv.usd)
    .div(priceOfBaseAsset.usd);

  let currentBoost: BigNumber;

  if (yearnGaugeBalance.isGreaterThan(0)) {
    currentBoost = yearnWorkingBalance.div(
      inverseMaxBoost.times(yearnGaugeBalance)
    );
    if (currentBoost.isNaN()) {
      currentBoost = new BigNumber(1);
    }
  } else {
    currentBoost = new BigNumber(MaxBoost);
  }

  const boostedApy = baseApy.times(currentBoost);

  const poolApr = new BigNumber((await calculatePoolApr(vault, ctx)) || 0);
  const poolApy = poolApr
    .div(365)
    .plus(1)
    .pow(365 - 1)
    .minus(1);

  const totalApy = boostedApy.plus(poolApy).toNumber();

  const feeDenominator = 1e4;

  const poolRewardsApy = 0; // TODO: investigate

  let keepCrv: number, totalPerformanceFee: number, managementFee: number;
  if (vault.type === "v1") {
    keepCrv = (vault.fees.special.keepCrv ?? 0) / feeDenominator;
    const performanceFee = vault.fees.general.performanceFee / feeDenominator;
    managementFee = 0;
    const other =
      (vault.fees.general.strategistReward + vault.fees.general.treasuryFee) /
      feeDenominator;
    totalPerformanceFee = performanceFee + other;
  } else {
    // v2
    keepCrv = (vault.fees.special.keepCrv ?? 0) / feeDenominator;
    const performanceFee = vault.fees.general.performanceFee / feeDenominator;
    managementFee = vault.fees.general.managementFee / feeDenominator;
    totalPerformanceFee = performanceFee * 2;
  }

  const netApy = boostedApy
    .times(1 - keepCrv)
    .plus(poolRewardsApy)
    .times(1 - totalPerformanceFee)
    .minus(managementFee)
    .plus(poolApy);

  const data = {
    baseApy: baseApy.toNumber(),
    currentBoost: currentBoost.toNumber(),
    boostedApy: boostedApy.toNumber(),
    poolApy: poolApy.toNumber(),
    netApy: netApy.toNumber(),
    keepCrv,
    totalPerformanceFee,
    totalApy,
  };

  const apy = {
    recommended: totalApy,
    type: "curve",
    composite: true,
    description: "Pool APY + Boosted CRV APY",
    data,
  };

  return apy;
}
