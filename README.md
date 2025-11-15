# new_summary_expriment
基于项目源码的摘要实验仓库

## 目录设计

- projects
  - baseline (注释部分函数后的源码)
      - openssl
      - ...
  - groundtruth (未注释的源码)
      - openssl
      - ...
- summaries
  - sa (静态分析摘要存放位置)
    - openssl
    - ...
  - llm (大模型摘要存放位置)
    - taint (污点类型摘要存放位置)
      - openssl
      - ...
    - memory (内存类型摘要存放位置)
      - openssl
      - ...
- scripts (实验驱动脚本)
- reports (codechecker分析输出的报告)
  - groundtruth
  - baseline
  - method
- intermediates (分析reports得到的中间结果)
  - groundtruth
  - baseline
  - method
- results (分析汇总结果)

## 结果形式设计

### intermediates形式设计



### results形式设计

## 脚本设计


