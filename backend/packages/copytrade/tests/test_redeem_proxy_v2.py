import contextlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade import redeem_proxy


class RedeemProxyV2Tests(unittest.TestCase):
    def _web3(self):
        try:
            from web3 import Web3
        except ImportError:  # pragma: no cover - covered by environment failures
            self.skipTest("web3 not installed")
        return Web3()

    def test_redeem_and_merge_route_to_v2_adapters_with_pusd_collateral(self):
        from web3 import Web3

        w3 = self._web3()
        condition_id = "0x" + "12" * 32

        redeem_to, redeem_data = redeem_proxy._build_redeem_calldata(w3, condition_id)
        redeem_contract = w3.eth.contract(address=Web3.to_checksum_address(redeem_to), abi=redeem_proxy.CTF_ABI)
        redeem_fn, redeem_args = redeem_contract.decode_function_input(redeem_data)

        self.assertEqual(redeem_to, redeem_proxy.CTF_COLLATERAL_ADAPTER_ADDRESS)
        self.assertEqual(redeem_fn.fn_name, "redeemPositions")
        self.assertEqual(redeem_args["collateralToken"].lower(), redeem_proxy.PUSD_ADDRESS.lower())

        merge_to, merge_data = redeem_proxy._build_merge_calldata(
            w3,
            condition_id,
            1.25,
            negative_risk=True,
        )
        merge_contract = w3.eth.contract(address=Web3.to_checksum_address(merge_to), abi=redeem_proxy.CTF_ABI)
        merge_fn, merge_args = merge_contract.decode_function_input(merge_data)

        self.assertEqual(merge_to, redeem_proxy.NEG_RISK_CTF_COLLATERAL_ADAPTER_ADDRESS)
        self.assertEqual(merge_fn.fn_name, "mergePositions")
        self.assertEqual(merge_args["collateralToken"].lower(), redeem_proxy.PUSD_ADDRESS.lower())
        self.assertEqual(merge_args["amount"], 1_250_000)

    def test_wrap_and_unwrap_calldata_uses_ramp_contracts_and_correct_approval_targets(self):
        from web3 import Web3

        w3 = self._web3()
        recipient = "0x" + "34" * 20
        amount_raw = 2_500_000

        approve_data = redeem_proxy._build_approve_calldata(
            w3,
            redeem_proxy.COLLATERAL_ONRAMP_ADDRESS,
            amount_raw,
        )
        usdc = w3.eth.contract(address=Web3.to_checksum_address(redeem_proxy.USDC_ADDRESS), abi=redeem_proxy.ERC20_ABI)
        approve_fn, approve_args = usdc.decode_function_input(approve_data)
        self.assertEqual(approve_fn.fn_name, "approve")
        self.assertEqual(approve_args["spender"].lower(), redeem_proxy.COLLATERAL_ONRAMP_ADDRESS.lower())
        self.assertEqual(approve_args["amount"], amount_raw)

        wrap_data = redeem_proxy._build_wrap_calldata(w3, recipient, amount_raw)
        onramp = w3.eth.contract(address=Web3.to_checksum_address(redeem_proxy.COLLATERAL_ONRAMP_ADDRESS), abi=redeem_proxy.PUSD_RAMP_ABI)
        wrap_fn, wrap_args = onramp.decode_function_input(wrap_data)
        self.assertEqual(wrap_fn.fn_name, "wrap")
        self.assertEqual(wrap_args["_asset"].lower(), redeem_proxy.USDC_ADDRESS.lower())
        self.assertEqual(wrap_args["_to"].lower(), recipient.lower())
        self.assertEqual(wrap_args["_amount"], amount_raw)

        pusd_approve_data = redeem_proxy._build_pusd_approve_calldata(
            w3,
            redeem_proxy.COLLATERAL_OFFRAMP_ADDRESS,
            amount_raw,
        )
        pusd = w3.eth.contract(address=Web3.to_checksum_address(redeem_proxy.PUSD_ADDRESS), abi=redeem_proxy.ERC20_ABI)
        pusd_approve_fn, pusd_approve_args = pusd.decode_function_input(pusd_approve_data)
        self.assertEqual(pusd_approve_fn.fn_name, "approve")
        self.assertEqual(pusd_approve_args["spender"].lower(), redeem_proxy.COLLATERAL_OFFRAMP_ADDRESS.lower())

        unwrap_data = redeem_proxy._build_unwrap_calldata(w3, recipient, amount_raw)
        offramp = w3.eth.contract(address=Web3.to_checksum_address(redeem_proxy.COLLATERAL_OFFRAMP_ADDRESS), abi=redeem_proxy.PUSD_RAMP_ABI)
        unwrap_fn, unwrap_args = offramp.decode_function_input(unwrap_data)
        self.assertEqual(unwrap_fn.fn_name, "unwrap")
        self.assertEqual(unwrap_args["_asset"].lower(), redeem_proxy.USDC_ADDRESS.lower())
        self.assertEqual(unwrap_args["_to"].lower(), recipient.lower())
        self.assertEqual(unwrap_args["_amount"], amount_raw)

    def test_dry_run_wrap_outputs_calldata_preview(self):
        args = SimpleNamespace(wrap_usdce=1.25, unwrap_pusd=None, dry_run=True)
        private_key = "0x" + "11" * 32
        owner = "0x" + "56" * 20

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = redeem_proxy._run_pusd_operation(
                private_key,
                lambda key: owner if key == "FUNDER_ADDRESS" else "",
                args,
            )

        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["action"], "wrap_usdce")
        self.assertEqual(payload["owner"].lower(), owner.lower())
        self.assertEqual(payload["amount_raw"], 1_250_000)
        self.assertEqual(payload["approval"]["token"], redeem_proxy.USDC_ADDRESS)
        self.assertEqual(payload["approval"]["spender"], redeem_proxy.COLLATERAL_ONRAMP_ADDRESS)
        self.assertEqual(payload["transaction"]["to"], redeem_proxy.COLLATERAL_ONRAMP_ADDRESS)

    def test_merge_pair_detection_preserves_negative_risk_metadata(self):
        pairs = redeem_proxy._find_mergeable_pairs(
            [
                {"conditionId": "cond", "outcomeIndex": 0, "size": "2.0", "mergeable": True, "negativeRisk": True},
                {"conditionId": "cond", "outcomeIndex": 1, "size": "1.5", "mergeable": True},
            ]
        )

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["amount"], 1.5)
        self.assertTrue(pairs[0]["negativeRisk"])

    def test_live_redeem_caps_merge_attempts_and_includes_merge_in_timeout_budget(self):
        args = SimpleNamespace(dry_run=False)
        merge_pairs = [
            {
                "conditionId": f"cond-{i}",
                "amount": 1.0,
                "title": f"market-{i}",
                "negativeRisk": True,
            }
            for i in range(22)
        ]
        redeemable = [{"conditionId": f"redeem-{i}", "outcome": "YES", "size": 1.0} for i in range(20)]
        attempted = []

        def merge_many(pairs):
            attempted.extend(pairs)
            return [{"pair": pair, "ok": True} for pair in pairs]

        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            patch.object(redeem_proxy, "MAX_MERGE_PER_RUN", 8),
            patch.object(redeem_proxy, "RELAY_EXECUTE_TIMEOUT_S", 45),
            patch.object(redeem_proxy, "_fetch_all_positions", return_value=[]),
            patch.object(redeem_proxy, "_find_mergeable_pairs", return_value=merge_pairs),
        ):
            code = redeem_proxy._execute_redeem_merge(
                "0xuser",
                redeemable,
                args,
                redeem_fn=lambda: [],
                merge_fn=lambda *_args, **_kwargs: None,
                merge_many_fn=merge_many,
            )

        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["mergeable"], 22)
        self.assertEqual(payload["merge_attempted"], 8)
        self.assertEqual(payload["merged"], 8)
        self.assertEqual(len(attempted), 8)
        self.assertEqual(payload["timeout_budget_s"], 45 * (20 + 8))

    def test_official_merge_many_batches_safe_transactions_once_and_falls_back(self):
        calls = []

        class Relay:
            def execute(self, txs, metadata=None):
                calls.append((len(txs), metadata))
                if len(calls) == 1:
                    raise RuntimeError("batch rejected")
                return SimpleNamespace(transaction_id="tx")

        pairs = [
            {"conditionId": f"cond-{i}", "amount": 1.0, "negativeRisk": False}
            for i in range(3)
        ]
        with patch.object(redeem_proxy, "_build_merge_safe_transaction", side_effect=lambda *_args, **_kwargs: object()):
            results = redeem_proxy._eoa_official_merge_many(object(), Relay(), pairs)

        self.assertEqual(calls[0], (3, "merge"))
        self.assertEqual(calls[1:], [(1, "merge"), (1, "merge"), (1, "merge")])
        self.assertEqual([r["ok"] for r in results], [True, True, True])


if __name__ == "__main__":
    unittest.main()
