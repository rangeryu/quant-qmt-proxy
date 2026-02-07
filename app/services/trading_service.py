"""
交易服务层
"""
import os
import sys
from datetime import datetime
from typing import List
from app.utils.logger import logger

# 添加xtquant包到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

try:
    import xtquant.xttrader as xttrader
    from xtquant import xtconstant
    XTQUANT_AVAILABLE = True
except ImportError as e:
    logger.error("xtquant模块未正确安装")
    XTQUANT_AVAILABLE = False
    # 创建模拟模块以避免导入错误
    class MockModule:
        def __getattr__(self, name):
            def mock_function(*args, **kwargs):
                raise NotImplementedError(f"xtquant模块未正确安装，无法调用 {name}")
            return mock_function
    
    xttrader = MockModule()
    xtconstant = MockModule()

from app.config import Settings, XTQuantMode
from app.models.trading_models import (
    AccountInfo,
    AccountType,
    AssetInfo,
    CancelOrderRequest,
    ConnectRequest,
    ConnectResponse,
    OrderRequest,
    OrderResponse,
    OrderStatus,
    PositionInfo,
    RiskInfo,
    StrategyInfo,
    TradeInfo,
)
from app.utils.exceptions import TradingServiceException
from app.utils.helpers import validate_stock_code
from app.utils.logger import logger


class TradingService:
    """交易服务类"""
    
    def __init__(self, settings: Settings):
        """初始化交易服务"""
        self.settings = settings
        self._initialized = False
        self._connected_accounts = {}
        self._orders = {}
        self._trades = {}
        self._order_counter = 1000
        self._try_initialize()
    
    def _try_initialize(self):
        """尝试初始化xttrader"""
        if not XTQUANT_AVAILABLE:
            self._initialized = False
            return
        
        if self.settings.xtquant.mode == XTQuantMode.MOCK:
            self._initialized = False
            return
        
        try:
            # 初始化xttrader
            # xttrader.connect()
            self._initialized = True
            logger.info("xttrader 已初始化")
        except Exception as e:
            logger.warning(f"xttrader 初始化失败: {e}")
            self._initialized = False
    
    def _should_use_real_trading(self) -> bool:
        """
        判断是否使用真实交易
        只有在 prod 模式且配置允许时才允许真实交易
        """
        return (
            self.settings.xtquant.mode == XTQuantMode.PROD and
            self.settings.xtquant.trading.allow_real_trading
        )
    
    def _should_use_real_data(self) -> bool:
        """
        判断是否连接xtquant获取真实数据（但不一定允许交易）
        dev 和 prod 模式都连接 xtquant
        """
        return (            
            self.settings.xtquant.mode in [XTQuantMode.DEV, XTQuantMode.PROD]
        )
    
    def _get_stock_account(self, session_id: str):
        """
        从 session 获取 StockAccount 对象
        用于调用 xtquant 查询接口
        """
        if not XTQUANT_AVAILABLE:
            return None
        try:
            from xtquant.xttype import StockAccount
            account_info = self._connected_accounts[session_id]["account_info"]
            return StockAccount(account_info.account_id)
        except Exception as e:
            logger.warning(f"创建 StockAccount 失败: {e}")
            return None
    
    def _convert_xt_position(self, xt_pos) -> PositionInfo:
        """
        将 XtPosition 转换为 PositionInfo
        XtPosition 字段参考 xttrader.md 文档第553-574行
        """
        return PositionInfo(
            stock_code=xt_pos.stock_code,
            stock_name=getattr(xt_pos, 'instrument_name', ''),
            volume=xt_pos.volume,
            available_volume=xt_pos.can_use_volume,
            frozen_volume=xt_pos.frozen_volume,
            cost_price=xt_pos.avg_price,
            market_price=getattr(xt_pos, 'last_price', 0.0),
            market_value=xt_pos.market_value,
            profit_loss=getattr(xt_pos, 'float_profit', 0.0),
            profit_loss_ratio=getattr(xt_pos, 'profit_rate', 0.0)
        )
    
    def _convert_xt_order(self, xt_order) -> OrderResponse:
        """
        将 XtOrder 转换为 OrderResponse
        XtOrder 字段参考 xttrader.md 文档第507-529行
        """
        # 映射 order_type 到买卖方向
        from xtquant import xtconstant
        side = "BUY"
        if hasattr(xtconstant, 'STOCK_SELL') and xt_order.order_type == xtconstant.STOCK_SELL:
            side = "SELL"
        elif xt_order.order_type in [24, 25]:  # 常见的卖出类型值
            side = "SELL"
        
        # 映射 price_type 到订单类型
        order_type = "LIMIT"
        if hasattr(xtconstant, 'LATEST_PRICE') and xt_order.price_type == xtconstant.LATEST_PRICE:
            order_type = "MARKET"
        
        # 映射 order_status
        status_map = {
            48: "PENDING",      # ORDER_UNREPORTED
            49: "PENDING",      # ORDER_WAIT_REPORTING
            50: "SUBMITTED",    # ORDER_REPORTED
            51: "SUBMITTED",    # ORDER_REPORTED_CANCEL
            52: "PARTIAL_FILLED",  # ORDER_PARTSUCC_CANCEL
            53: "CANCELLED",    # ORDER_PART_CANCEL
            54: "CANCELLED",    # ORDER_CANCELED
            55: "PARTIAL_FILLED",  # ORDER_PART_SUCC
            56: "FILLED",       # ORDER_SUCCEEDED
            57: "REJECTED",     # ORDER_JUNK
        }
        status = status_map.get(xt_order.order_status, "PENDING")
        
        # 处理时间戳
        submitted_time = datetime.now()
        if xt_order.order_time and xt_order.order_time > 0:
            try:
                submitted_time = datetime.fromtimestamp(xt_order.order_time)
            except Exception:
                pass
        
        return OrderResponse(
            order_id=str(xt_order.order_id),
            stock_code=xt_order.stock_code,
            side=side,
            order_type=order_type,
            volume=xt_order.order_volume,
            price=xt_order.price,
            status=status,
            submitted_time=submitted_time,
            filled_volume=xt_order.traded_volume,
            average_price=xt_order.traded_price if xt_order.traded_price > 0 else None
        )
    
    def _convert_xt_trade(self, xt_trade) -> TradeInfo:
        """
        将 XtTrade 转换为 TradeInfo
        XtTrade 字段参考 xttrader.md 文档第531-551行
        """
        # 映射 order_type 到买卖方向
        from xtquant import xtconstant
        side = "BUY"
        if hasattr(xtconstant, 'STOCK_SELL') and xt_trade.order_type == xtconstant.STOCK_SELL:
            side = "SELL"
        elif xt_trade.order_type in [24, 25]:
            side = "SELL"
        
        # 处理时间戳
        trade_time = datetime.now()
        if xt_trade.traded_time and xt_trade.traded_time > 0:
            try:
                trade_time = datetime.fromtimestamp(xt_trade.traded_time)
            except Exception:
                pass
        
        return TradeInfo(
            trade_id=str(xt_trade.traded_id),
            order_id=str(xt_trade.order_id),
            stock_code=xt_trade.stock_code,
            side=side,
            volume=xt_trade.traded_volume,
            price=xt_trade.traded_price,
            amount=xt_trade.traded_amount,
            trade_time=trade_time,
            commission=getattr(xt_trade, 'commission', 0.0)
        )
    
    def connect_account(self, request: ConnectRequest) -> ConnectResponse:
        """连接交易账户"""
        try:
            # 调用xttrader连接账户
            # account = xttrader.connect(request.account_id, request.password, request.client_id)
            
            # 模拟连接成功
            account_info = AccountInfo(
                account_id=request.account_id,
                account_type=AccountType.SECURITY,
                account_name=f"账户{request.account_id}",
                status="CONNECTED",
                balance=1000000.0,
                available_balance=950000.0,
                frozen_balance=50000.0,
                market_value=800000.0,
                total_asset=1800000.0
            )
            
            session_id = f"session_{request.account_id}_{datetime.now().timestamp()}"
            self._connected_accounts[session_id] = {
                "account_info": account_info,
                "connected_time": datetime.now()
            }
            
            return ConnectResponse(
                success=True,
                message="账户连接成功",
                session_id=session_id,
                account_info=account_info
            )
            
        except Exception as e:
            return ConnectResponse(
                success=False,
                message=f"账户连接失败: {str(e)}"
            )
    
    def disconnect_account(self, session_id: str) -> bool:
        """断开交易账户"""
        try:
            if session_id in self._connected_accounts:
                del self._connected_accounts[session_id]
                return True
            return False
        except Exception as e:
            raise TradingServiceException(f"断开账户失败: {str(e)}")
    
    def get_account_info(self, session_id: str) -> AccountInfo:
        """获取账户信息"""
        if session_id not in self._connected_accounts:
            raise TradingServiceException("账户未连接")
        
        return self._connected_accounts[session_id]["account_info"]
    
    def get_positions(self, session_id: str) -> List[PositionInfo]:
        """获取持仓信息"""
        if session_id not in self._connected_accounts:
            raise TradingServiceException("账户未连接")
        
        # 尝试获取真实数据
        if self._should_use_real_data() and self._initialized:
            try:
                account = self._get_stock_account(session_id)
                if account:
                    from xtquant.xttrader import XtQuantTrader
                    # 使用已初始化的 trader 实例查询持仓
                    positions = xttrader.query_stock_positions(account)
                    if positions is not None:
                        logger.info(f"获取真实持仓数据成功，共 {len(positions)} 条")
                        return [self._convert_xt_position(p) for p in positions]
                    else:
                        logger.info("查询持仓返回空列表")
                        return []
            except Exception as e:
                logger.warning(f"获取真实持仓失败，降级为mock数据: {e}")
        
        # Mock 模式或真实查询失败时返回模拟数据
        mock_positions = [
            PositionInfo(
                stock_code="000001.SZ",
                stock_name="平安银行",
                volume=10000,
                available_volume=10000,
                frozen_volume=0,
                cost_price=12.50,
                market_price=13.20,
                market_value=132000.0,
                profit_loss=7000.0,
                profit_loss_ratio=0.056
            ),
            PositionInfo(
                stock_code="000002.SZ",
                stock_name="万科A",
                volume=5000,
                available_volume=5000,
                frozen_volume=0,
                cost_price=18.80,
                market_price=19.50,
                market_value=97500.0,
                profit_loss=3500.0,
                profit_loss_ratio=0.037
            )
        ]
        
        return mock_positions
    
    def submit_order(self, session_id: str, request: OrderRequest) -> OrderResponse:
        """提交订单"""
        if session_id not in self._connected_accounts:
            raise TradingServiceException("账户未连接")
        
        try:
            if not validate_stock_code(request.stock_code):
                raise TradingServiceException(f"无效的股票代码: {request.stock_code}")
            
            # 🔒 关键拦截点：检查是否允许真实交易
            if not self._should_use_real_trading():
                logger.warning(f"当前模式[{self.settings.xtquant.mode.value}]不允许真实交易，返回模拟订单")
                return self._get_mock_order_response(request)
            
            # ✅ 允许真实交易，调用xttrader提交订单
            logger.info(f"真实交易模式：提交订单 {request.stock_code} {request.side.value} {request.volume}股")
            
            order_id = xttrader.order_stock(
                session_id,
                request.stock_code,
                request.side.value,
                request.volume,
                request.price,
                request.order_type.value
            )
            
            order_response = OrderResponse(
                order_id=order_id,
                stock_code=request.stock_code,
                side=request.side.value,
                order_type=request.order_type.value,
                volume=request.volume,
                price=request.price,
                status=OrderStatus.SUBMITTED.value,
                submitted_time=datetime.now()
            )
            
            self._orders[order_id] = order_response
            
            return order_response
            
        except Exception as e:
            raise TradingServiceException(f"提交订单失败: {str(e)}")
    
    def _get_mock_order_response(self, request: OrderRequest) -> OrderResponse:
        """生成模拟订单响应"""
        order_id = f"mock_order_{self._order_counter}"
        self._order_counter += 1
        
        order_response = OrderResponse(
            order_id=order_id,
            stock_code=request.stock_code,
            side=request.side.value,
            order_type=request.order_type.value,
            volume=request.volume,
            price=request.price,
            status=OrderStatus.SUBMITTED.value,
            submitted_time=datetime.now()
        )
        
        self._orders[order_id] = order_response
        return order_response
    
    def cancel_order(self, session_id: str, request: CancelOrderRequest) -> bool:
        """撤销订单（dev/mock模式下总是拦截并返回True）"""
        if session_id not in self._connected_accounts:
            raise TradingServiceException("账户未连接")
        
        # dev/mock模式下直接拦截，始终返回True
        if not self._should_use_real_trading():
            logger.warning(f"当前模式[{self.settings.xtquant.mode.value}]不允许真实交易，撤单请求已拦截，直接返回True")
            # 如果有订单，标记为已撤销
            if request.order_id in self._orders:
                self._orders[request.order_id].status = OrderStatus.CANCELLED.value
            return True
        
        # prod模式下才做真实撤单校验
        try:
            if request.order_id not in self._orders:
                raise TradingServiceException("订单不存在")
            logger.info(f"真实交易模式：撤销订单 {request.order_id}")
            success = xttrader.cancel_order_stock(session_id, request.order_id)
            if success and request.order_id in self._orders:
                self._orders[request.order_id].status = OrderStatus.CANCELLED.value
            return success
        except Exception as e:
            raise TradingServiceException(f"撤销订单失败: {str(e)}")
    
    def get_orders(self, session_id: str) -> List[OrderResponse]:
        """获取订单列表"""
        if session_id not in self._connected_accounts:
            raise TradingServiceException("账户未连接")
        
        # 尝试获取真实数据
        if self._should_use_real_data() and self._initialized:
            try:
                account = self._get_stock_account(session_id)
                if account:
                    orders = xttrader.query_stock_orders(account, False)
                    if orders is not None:
                        logger.info(f"获取真实订单数据成功，共 {len(orders)} 条")
                        return [self._convert_xt_order(o) for o in orders]
                    else:
                        logger.info("查询订单返回空，回退到内存订单")
            except Exception as e:
                logger.warning(f"获取真实订单失败，降级为内存订单: {e}")
        
        # Mock 模式或真实查询失败时返回内存中的订单
        return list(self._orders.values())
    
    def get_trades(self, session_id: str) -> List[TradeInfo]:
        """获取成交记录"""
        if session_id not in self._connected_accounts:
            raise TradingServiceException("账户未连接")
        
        # 尝试获取真实数据
        if self._should_use_real_data() and self._initialized:
            try:
                account = self._get_stock_account(session_id)
                if account:
                    trades = xttrader.query_stock_trades(account)
                    if trades is not None:
                        logger.info(f"获取真实成交数据成功，共 {len(trades)} 条")
                        return [self._convert_xt_trade(t) for t in trades]
                    else:
                        logger.info("查询成交返回空列表")
                        return []
            except Exception as e:
                logger.warning(f"获取真实成交失败，降级为mock数据: {e}")
        
        # Mock 模式或真实查询失败时返回模拟数据
        mock_trades = [
            TradeInfo(
                trade_id="trade_001",
                order_id="order_1001",
                stock_code="000001.SZ",
                side="BUY",
                volume=1000,
                price=13.20,
                amount=13200.0,
                trade_time=datetime.now(),
                commission=13.20
            )
        ]
        
        return mock_trades
    
    def get_asset_info(self, session_id: str) -> AssetInfo:
        """获取资产信息"""
        if session_id not in self._connected_accounts:
            raise TradingServiceException("账户未连接")
        
        # 尝试获取真实数据
        if self._should_use_real_data() and self._initialized:
            try:
                account = self._get_stock_account(session_id)
                if account:
                    asset = xttrader.query_stock_asset(account)
                    if asset is not None:
                        logger.info(f"获取真实资产数据成功")
                        # XtAsset 字段: cash, frozen_cash, market_value, total_asset, fetch_balance
                        return AssetInfo(
                            total_asset=asset.total_asset,
                            market_value=asset.market_value,
                            cash=asset.cash,
                            frozen_cash=asset.frozen_cash,
                            available_cash=asset.cash,  # 可用金额
                            profit_loss=0.0,  # XtAsset 不包含盈亏信息，需要从持仓计算
                            profit_loss_ratio=0.0
                        )
                    else:
                        logger.info("查询资产返回空")
            except Exception as e:
                logger.warning(f"获取真实资产失败，降级为mock数据: {e}")
        
        # Mock 模式或真实查询失败时返回模拟数据
        return AssetInfo(
            total_asset=1800000.0,
            market_value=800000.0,
            cash=950000.0,
            frozen_cash=50000.0,
            available_cash=900000.0,
            profit_loss=50000.0,
            profit_loss_ratio=0.028
        )
    
    def get_risk_info(self, session_id: str) -> RiskInfo:
        """获取风险信息"""
        if session_id not in self._connected_accounts:
            raise TradingServiceException("账户未连接")
        
        try:
            # 这里可以添加风险计算逻辑
            return RiskInfo(
                position_ratio=0.44,  # 持仓比例
                cash_ratio=0.56,      # 现金比例
                max_drawdown=0.05,    # 最大回撤
                var_95=0.02,          # 95% VaR
                var_99=0.03           # 99% VaR
            )
            
        except Exception as e:
            raise TradingServiceException(f"获取风险信息失败: {str(e)}")
    
    def get_strategies(self, session_id: str) -> List[StrategyInfo]:
        """获取策略列表"""
        if session_id not in self._connected_accounts:
            raise TradingServiceException("账户未连接")
        
        try:
            # 模拟策略数据
            mock_strategies = [
                StrategyInfo(
                    strategy_name="MA策略",
                    strategy_type="TREND_FOLLOWING",
                    status="RUNNING",
                    created_time=datetime.now(),
                    last_update_time=datetime.now(),
                    parameters={"period": 20, "threshold": 0.02}
                ),
                StrategyInfo(
                    strategy_name="均值回归策略",
                    strategy_type="MEAN_REVERSION",
                    status="STOPPED",
                    created_time=datetime.now(),
                    last_update_time=datetime.now(),
                    parameters={"lookback": 10, "entry_threshold": 0.05}
                )
            ]
            
            return mock_strategies
            
        except Exception as e:
            raise TradingServiceException(f"获取策略列表失败: {str(e)}")
    
    def is_connected(self, session_id: str) -> bool:
        """检查账户是否连接"""
        return session_id in self._connected_accounts
