# CoinTaxman
# Copyright (C) 2021  Carsten Docktor <https://github.com/provinzio>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import csv
import datetime
import decimal
from pathlib import Path
from typing import Optional, Type

import balance_queue
import config
import core
import log_config
import misc
import transaction as tr
from book import Book
from price_data import PriceData

log = log_config.getLogger(__name__)

TAX_DEADLINE = min(
    datetime.datetime(config.TAX_YEAR, 12, 31, 23, 59, 59), datetime.datetime.now()
)


def in_tax_year(op: tr.Operation) -> bool:
    return op.utc_time.year == config.TAX_YEAR


class Taxman:
    def __init__(self, book: Book, price_data: PriceData) -> None:
        self.book = book
        self.price_data = price_data

        self.tax_events: list[tr.TaxEvent] = []
        # Tax Events which would occur if all left over coins were sold now.
        self.virtual_tax_events: list[tr.TaxEvent] = []

        # Determine used functions/classes depending on the config.
        country = config.COUNTRY.name
        try:
            self.__evaluate_taxation = getattr(self, f"_evaluate_taxation_{country}")
        except AttributeError:
            raise NotImplementedError(f"Unable to evaluate taxation for {country=}.")

        if config.PRINCIPLE == core.Principle.FIFO:
            # Explicity define type for BalanceType on first declaration
            # to avoid mypy errors.
            self.BalanceType: Type[
                balance_queue.BalanceQueue
            ] = balance_queue.BalanceFIFOQueue
        elif config.PRINCIPLE == core.Principle.LIFO:
            self.BalanceType = balance_queue.BalanceLIFOQueue
        else:
            raise NotImplementedError(
                f"Unable to evaluate taxation for {config.PRINCIPLE=}."
            )

    @staticmethod
    def in_tax_year(op: tr.Operation) -> bool:
        return op.utc_time.year == config.TAX_YEAR

    @staticmethod
    def tax_deadline() -> datetime.datetime:
        return min(
            datetime.datetime(config.TAX_YEAR, 12, 31, 23, 59, 59),
            datetime.datetime.now(),
        ).astimezone()

    def _evaluate_taxation_GERMANY(
        self,
        coin: str,
        operations: list[tr.Operation],
    ) -> None:
        balance = self.BalanceType()

        def evaluate_sell(
            op: tr.Operation, force: bool = False
        ) -> Optional[tr.TaxEvent]:
            # Remove coins from queue.
            sold_coins = balance.remove(op)

            if coin == config.FIAT:
                # Not taxable.
                return None

            if not in_tax_year(op) and not force:
                # Sell is only taxable in the respective year.
                return None

            taxation_type = "Sonstige Einkünfte"
            # Price of the sell.
            sell_value = self.price_data.get_cost(op)
            taxed_gain = decimal.Decimal()
            real_gain = decimal.Decimal()
            # Coins which are older than (in this case) one year or
            # which come from an Airdrop, CoinLend or Commission (in an
            # foreign currency) will not be taxed.
            for sc in sold_coins:
                is_taxable = not config.IS_LONG_TERM(
                    sc.op.utc_time, op.utc_time
                ) and not (
                    isinstance(
                        sc.op,
                        (
                            tr.Airdrop,
                            tr.CoinLendInterest,
                            tr.StakingInterest,
                            tr.Commission,
                        ),
                    )
                    and not sc.op.coin == config.FIAT
                )
                # Only calculate the gains if necessary.
                if is_taxable or config.CALCULATE_UNREALIZED_GAINS:
                    partial_sell_value = (sc.sold / op.change) * sell_value
                    sold_coin_cost = self.price_data.get_cost(sc)
                    gain = partial_sell_value - sold_coin_cost
                    if is_taxable:
                        taxed_gain += gain
                    if config.CALCULATE_UNREALIZED_GAINS:
                        real_gain += gain
            remark = ", ".join(
                f"{sc.sold} from {sc.op.utc_time} " f"({sc.op.__class__.__name__})"
                for sc in sold_coins
            )
            return tr.TaxEvent(
                taxation_type,
                taxed_gain,
                op,
                sell_value,
                real_gain,
                remark,
            )

        # TODO handle buy.fees and sell.fees.

        for op in operations:
            if isinstance(op, tr.Fee):
                raise RuntimeError("single fee operations shouldn't exist")
                balance.remove_fee(op.change)
                if in_tax_year(op):
                    # Fees reduce taxed gain.
                    taxation_type = "Sonstige Einkünfte"
                    taxed_gain = -self.price_data.get_cost(op)
                    tx = tr.TaxEvent(taxation_type, taxed_gain, op)
                    self.tax_events.append(tx)
            elif isinstance(op, tr.CoinLend):
                pass
            elif isinstance(op, tr.CoinLendEnd):
                pass
            elif isinstance(op, tr.Staking):
                pass
            elif isinstance(op, tr.StakingEnd):
                pass
            elif isinstance(op, tr.Buy):
                balance.add(op)
            elif isinstance(op, tr.Sell):
                if tx_ := evaluate_sell(op):
                    self.tax_events.append(tx_)
            elif isinstance(op, (tr.CoinLendInterest, tr.StakingInterest)):
                balance.add(op)
                if in_tax_year(op):
                    if misc.is_fiat(coin):
                        assert not isinstance(
                            op, tr.StakingInterest
                        ), "You can not stake fiat currencies."
                        taxation_type = "Einkünfte aus Kapitalvermögen"
                    else:
                        taxation_type = "Einkünfte aus sonstigen Leistungen"
                    taxed_gain = self.price_data.get_cost(op)
                    tx = tr.TaxEvent(taxation_type, taxed_gain, op)
                    self.tax_events.append(tx)
            elif isinstance(op, tr.Airdrop):
                balance.add(op)
            elif isinstance(op, tr.Commission):
                balance.add(op)
                if in_tax_year(op):
                    taxation_type = "Einkünfte aus sonstigen Leistungen"
                    taxed_gain = self.price_data.get_cost(op)
                    tx = tr.TaxEvent(taxation_type, taxed_gain, op)
                    self.tax_events.append(tx)
            elif isinstance(op, tr.Deposit):
                if coin == config.FIAT:
                    # Add to balance;
                    # we do not care, where our home fiat comes from.
                    balance.add(op)
                else:  # coin != config.FIAT
                    log.warning(
                        f"Unresolved deposit of {op.change} {coin} "
                        f"on {op.platform} at {op.utc_time}. "
                        "The evaluation might be wrong."
                    )
            elif isinstance(op, tr.Withdrawal):
                if coin == config.FIAT:
                    # Remove from balance;
                    # we do not care, where our home fiat goes to.
                    balance.remove(op)
                else:  # coin != config.FIAT
                    log.warning(
                        f"Unresolved withdrawal of {op.change} {coin} "
                        f"from {op.platform} at {op.utc_time}. "
                        "The evaluation might be wrong."
                    )
            else:
                raise NotImplementedError

        balance.sanity_check()

        # Calculate the amount of coins which should be left on the platform
        # and evaluate the (taxed) gain, if the coin would be sold right now.
        if config.CALCULATE_UNREALIZED_GAINS and (
            (left_coin := misc.dsum((bop.not_sold for bop in balance.queue)))
        ):
            assert isinstance(left_coin, decimal.Decimal)
            # Calculate unrealized gains for the last time of `TAX_YEAR`.
            # If we are currently in ´TAX_YEAR` take now.
            virtual_sell = tr.Sell(
                TAX_DEADLINE,
                op.platform,
                left_coin,
                coin,
                [-1],
                Path(""),
            )
            if tx_ := evaluate_sell(virtual_sell, force=True):
                self.virtual_tax_events.append(tx_)

    def _evaluate_taxation_per_coin(
        self,
        operations: list[tr.Operation],
    ) -> None:
        """Evaluate the taxation for a list of operations per coin using
        country specific functions.

        Args:
            operations (list[tr.Operation])
        """
        for coin, coin_operations in misc.group_by(operations, "coin").items():
            coin_operations = tr.sort_operations(coin_operations, ["utc_time"])
            self.__evaluate_taxation(coin, coin_operations)

    def evaluate_taxation(self) -> None:
        """Evaluate the taxation using country specific function."""
        log.debug("Starting evaluation...")

        assert all(
            op.utc_time.year <= config.TAX_YEAR for op in self.book.operations
        ), "For tax evaluation, no operation should happen after the tax year."

        if config.MULTI_DEPOT:
            # Evaluate taxation separated by platforms and coins.
            for _, operations in misc.group_by(
                self.book.operations, "platform"
            ).items():
                self._evaluate_taxation_per_coin(operations)
        else:
            # Evaluate taxation separated by coins "in a single virtual depot".
            self._evaluate_taxation_per_coin(self.book.operations)

    def print_evaluation(self) -> None:
        """Print short summary of evaluation to stdout."""
        eval_str = "Evaluation:\n\n"

        # Summarize the tax evaluation.
        if self.tax_events:
            eval_str += f"Your tax evaluation for {config.TAX_YEAR}:\n"
            for taxation_type, tax_events in misc.group_by(
                self.tax_events, "taxation_type"
            ).items():
                taxed_gains = misc.dsum(tx.taxed_gain for tx in tax_events)
                eval_str += f"{taxation_type}: {taxed_gains:.2f} {config.FIAT}\n"
        else:
            eval_str += (
                "Either the evaluation has not run or there are no tax events "
                f"for {config.TAX_YEAR}.\n"
            )

        # Summarize the virtual sell, if all left over coins would be sold right now.
        if self.virtual_tax_events:
            assert config.CALCULATE_UNREALIZED_GAINS
            latest_operation = max(
                self.virtual_tax_events, key=lambda tx: tx.op.utc_time
            )
            lo_date = latest_operation.op.utc_time.strftime("%d.%m.%y")

            invested = misc.dsum(tx.sell_value for tx in self.virtual_tax_events)
            real_gains = misc.dsum(tx.real_gain for tx in self.virtual_tax_events)
            taxed_gains = misc.dsum(tx.taxed_gain for tx in self.virtual_tax_events)
            eval_str += "\n"
            eval_str += (
                f"Deadline {config.TAX_YEAR}: {lo_date}\n"
                f"You were invested with {invested:.2f} {config.FIAT}.\n"
                f"If you would have sold everything then, "
                f"you would have realized {real_gains:.2f} {config.FIAT} gains "
                f"({taxed_gains:.2f} {config.FIAT} taxed gain).\n"
            )

            eval_str += "\n"
            eval_str += f"Your portfolio on {lo_date} was:\n"
            for tx in sorted(
                self.virtual_tax_events,
                key=lambda tx: tx.sell_value,
                reverse=True,
            ):
                eval_str += (
                    f"{tx.op.platform}: "
                    f"{tx.op.change:.6f} {tx.op.coin} > "
                    f"{tx.sell_value:.2f} {config.FIAT} "
                    f"({tx.real_gain:.2f} gain, {tx.taxed_gain:.2f} taxed gain)\n"
                )

        log.info(eval_str)

    def export_evaluation_as_csv(self) -> Path:
        """Export detailed summary of all tax events to CSV.

        File will be placed in export/ with ascending revision numbers
        (in case multiple evaluations will be done).

        When no tax events occured, the CSV will be exported only with
        a header line.

        Returns:
            Path: Path to the exported file.
        """
        file_path = misc.get_next_file_path(
            config.EXPORT_PATH, str(config.TAX_YEAR), "csv"
        )

        with open(file_path, "w", newline="", encoding="utf8") as f:
            writer = csv.writer(f)
            # Add embedded metadata info
            writer.writerow(
                ["# software", "CoinTaxman <https://github.com/provinzio/CoinTaxman>"]
            )
            commit_hash = misc.get_current_commit_hash(default="undetermined")
            writer.writerow(["# commit", commit_hash])
            writer.writerow(["# updated", datetime.date.today().strftime("%x")])

            header = [
                "Date and Time UTC",
                "Platform",
                "Taxation Type",
                f"Taxed Gain in {config.FIAT}",
                "Action",
                "Amount",
                "Asset",
                f"Sell Value in {config.FIAT}",
                "Remark",
            ]
            writer.writerow(header)
            # Tax events are currently sorted by coin. Sort by time instead.
            for tx in sorted(self.tax_events, key=lambda tx: tx.op.utc_time):
                line = [
                    tx.op.utc_time.strftime("%Y-%m-%d %H:%M:%S"),
                    tx.op.platform,
                    tx.taxation_type,
                    tx.taxed_gain,
                    tx.op.__class__.__name__,
                    tx.op.change,
                    tx.op.coin,
                    tx.sell_value,
                    tx.remark,
                ]
                writer.writerow(line)

        log.info("Saved evaluation in %s.", file_path)
        return file_path
