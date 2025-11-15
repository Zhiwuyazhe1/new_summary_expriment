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
- sources (项目源码压缩包，由脚本解压到指定的目录，或者尝试直接抹除该文件(比如修改文件名，添加后缀_deleted))
- results (分析汇总结果)
- configs (脚本相关的配置)

## 结果形式设计

### intermediates形式设计

- 最外部分类为项目
- 项目内部按照文件(从项目根目录开始的相对路径)进行分类
- 文件内部报告 checker, message, line 来标记每一个报告
- 最后是整个项目的分析时间

设计成json格式，对应如下：

### results形式设计

|project_name|tp|fp|fn|pre|rec|

最后一行为汇总，项目名为all

文件命名  date.csv

## 配置文件设计

- config.json
  - disabled_projects
    - 项目名数组，表明禁用分析的项目
  - baseline
    - 对象数组，对象为 "project_name" : "file_name" 的形式，表示要注释掉的文件

## 脚本设计

- main.py
  - 主流程脚本
  - 支持groundtruth(纯源码分析) baseline(屏蔽某个文件源码，无摘要分析) method(屏蔽某个文件源码，有摘要分析)三种模式
  - 支持llm(大模型摘要)/sa(静态分析摘要)模式，依赖于上面的模式，只针对method模式生效
  - 在llm模式下，支持taint/memory模式
  - 支持单独启用codechecker_driver/extractor/comparator这三个脚本
- extractor.py
  - 提取每个项目每个文件的分析结果和整个项目的分析耗时，转换成中间结果
  - 输入
    - 项目对应的reports的位置
    - 需要输出的项目对应的中间结果的位置
  - 输出
    - 中间结果
- comparator.py
  - 比较脚本，比较中间结果，并得到最终结果
  - 输入
    - 项目对应的 ground_truth 中间结果的目录
    - 项目对应的 待比对 中间结果的目录
  - 输出
    - 最终的result结果
- codechecker_driver.py
  - 驱动codechecker分析单个项目
  - 输入
    - compile_commands.json的位置
    - 输出reports的位置
    - codechecker配置文件的位置
  - 输出
    - codechecker的默认输出，即分析结果reports
    - 分析耗时
- environment.py (可以先尝试修改文件名对于codechecker分析的影响)
  - 项目环境形成脚本，主要是将源码从sources中解压到projects中，并针对baseline中的源码按照配置修改掉对应的文件名
  - 共有解压、注释（修改文件名）、还原（将修改的文件名复原）、compile_commands.json生成四种模式，可以自主选择启用的模式


