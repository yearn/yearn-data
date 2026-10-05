"""Known incident adjustments for economic yield reporting.

The raw StrategyReported events are kept unchanged. These rules only affect
analysis outputs that aim to represent economic yield rather than transient
vault accounting during public incidents.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IncidentAdjustment:
    incident_id: str
    classification: str
    description: str
    disclosure_url: str
    adjusted_gain_raw: str = "0"
    adjusted_loss_raw: str = "0"

    @property
    def adjusted_net_raw(self) -> str:
        return str(int(self.adjusted_gain_raw) - int(self.adjusted_loss_raw))


INCIDENT_ADJUSTMENTS: dict[str, IncidentAdjustment] = {
    # 2021-04-02: StrategyMakerYFIDAIDelegate reported loose YFI as profit.
    # The migration/fixed strategy then offset the erroneous profit as loss.
    "0x0603adc7020c93dfa207b9cc00d4474fb6767ae2f0caf1aa7db64bf23cd67822": IncidentAdjustment(
        incident_id="yearn-2021-04-02-yfi-maker-accounting",
        classification="phantom_profit",
        description="StrategyMakerYFIDAIDelegate reported loose YFI as profit before migration.",
        disclosure_url="https://github.com/yearn/yearn-security/blob/master/disclosures/2021-04-02.md",
    ),
    "0xc4d8412d70bdf5b5eea4d3b0117662b1be60b8b2c1475c82c52af529c0d50383": IncidentAdjustment(
        incident_id="yearn-2021-04-02-yfi-maker-accounting",
        classification="offsetting_paper_loss",
        description="Migration/fixed StrategyMakerYFIDAIDelegate offset the prior phantom YFI profit.",
        disclosure_url="https://github.com/yearn/yearn-security/blob/master/disclosures/2021-04-02.md",
    ),
    # 2021-05-14: SingleSidedCrvDAI simulated a full Curve unwind and reported
    # that simulated slippage as loss; the fixed strategy later offset it.
    "0x5558d40c511524b015cd307a134b3358f52326cf39c2c6e61604d5726c64dfdd": IncidentAdjustment(
        incident_id="yearn-2021-05-14-dai-curve-paper-loss",
        classification="paper_loss",
        description="SingleSidedCrvDAI reported simulated full-withdrawal slippage as loss.",
        disclosure_url="https://github.com/yearn/yearn-security/blob/master/disclosures/2021-05-14.md",
    ),
    "0x66847f4dc80a6b4c32666972a9a68416d802d78b54619503fd0aec358fedb185": IncidentAdjustment(
        incident_id="yearn-2021-05-14-dai-curve-paper-loss",
        classification="offsetting_phantom_profit",
        description="Fixed SingleSidedCrvDAI harvest offset the prior paper loss.",
        disclosure_url="https://github.com/yearn/yearn-security/blob/master/disclosures/2021-05-14.md",
    ),
    # 2021-05-20: StrategyMakerETHDAIDelegate falsely reported a WETH loss
    # after a target collateralization-ratio change; migration later offset it.
    "0x09d08d6a2caeb6f4f1c90ac68adea93f758dc57d173acfa6621d92f3dc3277d0": IncidentAdjustment(
        incident_id="yearn-2021-05-20-weth-maker-accounting",
        classification="paper_loss",
        description="StrategyMakerETHDAIDelegate falsely reported loss after c-ratio accounting bug.",
        disclosure_url="https://github.com/yearn/yearn-security/blob/master/disclosures/2021-05-20.md",
    ),
    "0xc0ce9effb23616906fb04c5400764fc5f036c2977939c976e959aad23326fb55": IncidentAdjustment(
        incident_id="yearn-2021-05-20-weth-maker-accounting",
        classification="offsetting_phantom_profit",
        description="Patched StrategyMakerETHDAIDelegate harvest offset the prior paper loss.",
        disclosure_url="https://github.com/yearn/yearn-security/blob/master/disclosures/2021-05-20.md",
    ),
    # 2021-10-28: yRecoverer intentionally reported recovered yPool LP tokens
    # as a vault loss while transferring the tokens to the Yearn multisig.
    "0x9cc9c42e2da1a2ae774a6ac393aff5974462c999773ea0b04cdd41aebdf5d650": IncidentAdjustment(
        incident_id="yearn-2021-10-28-yrecover-accounting",
        classification="intentional_recovery_accounting_loss",
        description="yRecoverer transferred recovered yPool LP tokens to the Yearn multisig while reporting them as loss.",
        disclosure_url=(
            "https://github.com/yearn/chief-multisig-officer/blob/"
            "1a139b7460f43e67dd2e88605a42ba72d0e0d701/scripts/ychad.py#L4596-L4625"
        ),
    ),
}


def adjustment_for_tx(tx_hash: str) -> IncidentAdjustment | None:
    return INCIDENT_ADJUSTMENTS.get(tx_hash.lower())
